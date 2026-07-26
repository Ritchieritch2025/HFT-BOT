# LATENCY_FACTS — 实测延迟事实档(W-LAT-BENCH-01)

**Status: ACTIVE — 本文件是全项目延迟假设的唯一引用源(费用墙之外的第二面墙)。**
**Origin:** W-LAT-BENCH-01(计划 `docs/PLAN_LATENCY_BENCH_ORDER_CANCEL_2026-07-16.md`,
操作员 2026-07-16 修订版:Tier 1 / 2a / 2b / 3,无生产订单,不碰生产采集机)。
**规则:**每个数字带测点、环境、时间戳、样本量。未实测的参数只能按
`UNMEASURED_CONSERVATIVE` 记账(取最近实测上界再加保守余量,写明假设),
不许拍脑袋。backtest 引用本文件,不引用别处。

## 一行裁决

✅ Tier 1 实测完成:签名 p99 = 453µs(<1ms,不触发优化债)。
✅ Tier 2a 实测完成:Mac→生产鉴权 GET 全回合 p50 ≈ 34ms(4 窗 n=1,600,0 错误)。
✅ Tier 3 实测完成(操作员 2026-07-16 开机 W09 解除阻塞):W09(us-east-2)
p50 = 6.2ms vs 同时刻 Mac 34.5ms——**搬家白捡 ~28ms**(§2b/§6)。
❌ Tier 2b 维持 BLOCKED(操作员裁决 2026-07-16):不建 demo 支持,
下单/撤单客户端正确性验证并入将来的 Tier 4(§5)。

汇总图:`docs/research_reports/LATENCY_BENCH_01_FIG.svg`(Tier 1 + Tier 2a 一图)。

## 1. Tier 1 — 本地软件路径(引擎内部,纯 CPU,零网络)

测点:Mac(RitcardodeMacBook-Pro.local,arm64,-O2)。
时间:2026-07-16T15:28:50Z。n=10,000/项。key = 生产 RSA-2048(签名成本
依赖 key 位数,ndjson 里 key_source/key_bits 字段防止混数)。
工具:`build/bench_engine`(apps/bench_engine.cpp,冒烟测试
tests/test_engine_bench.cpp 已挂入 `make check`)。
原始数据:`work/latency_baseline/engine_bench.ndjson`。

| 环节 | p50 | p90 | p99 | max |
|---|---|---|---|---|
| ExecPayload 构造(payload_build) | 0.04µs | 0.04µs | 0.04µs | 0.17µs |
| 订单 JSON 序列化(order_json) | 0.29µs | 0.33µs | 0.67µs | 16.8µs |
| RSA-PSS 签名+组包(sign_request) | **282.6µs** | 304.0µs | **452.8µs** | 3.58ms |
| 整链:意图→已签名请求(chain) | 282.5µs | 293.2µs | 352.2µs | 4.95ms |

结论:
- 本地软件路径完全由 RSA-PSS 签名主导(其余环节合计 <1µs)。
- 签名 p99 = 453µs < 1ms ⇒ 按验收标准**不登记优化债**。预签名/会话密钥
  优化存在但当前不值得做——网络 RTT(30ms 级)是 100 倍量级的大头。
- max 3.6-5ms 的尾部是 Mac 调度抖动(非专核、非实时优先级),EC2 部署时
  另测(见 §5 Tier 3)。
- 决策环节旁证(此前已实测,引用):WS decode+book apply ≈ 282ns/msg
  (bench_ws_decode,SESSION_LOG 2026-07-06 条目,原文第 3699 行附近)。

## 2. Tier 2a — 生产只读鉴权 GET 全回合(网络+TLS+鉴权+服务端)

测点:Mac(住宅网络)→ `external-api.kalshi.com`(生产)。
工具:`tools/latency_probe_auth.py`(GET 常量写死,不可变异;
2 req/s ≈ 11 read-token/s,占 300/s 读桶的 3.7%,生产管线无感)。
RTT 口径:单条热 HTTPS 连接上,请求发出→响应体读完;签名子进程耗时
单独记(sign_ms),**不计入** rtt_ms。
原始数据:`work/latency_baseline/tier2a_samples.ndjson`(每样本一行)。

窗口(UTC,2026-07-16,各 n=400,零错误、全 200):

| 窗口 | 端点 | n | min | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|---|
| w1 15:24-15:28 | /exchange/status | 199 | 31.2 | 34.0 | 36.5 | 39.3 | 40.1 |
| w1 15:24-15:28 | /markets?limit=1 | 200 | 34.2 | 37.8 | 42.4 | 79.9 | 86.8 |
| w2 16:15-16:18 | /exchange/status | 199 | 32.3 | 34.7 | 37.7 | 44.5 | 46.2 |
| w2 16:15-16:18 | /markets?limit=1 | 200 | 35.4 | 38.8 | 42.7 | 47.3 | 47.5 |
| w3 17:00-17:04 | /exchange/status | 199 | 32.0 | 35.5 | 39.8 | 59.4 | 94.7 |
| w3 17:00-17:04 | /markets?limit=1 | 200 | 36.1 | 39.6 | 44.4 | 83.7 | 114.0 |
| w4 17:13-17:16(与 W09 同时刻) | /exchange/status | 199 | 31.8 | 34.5 | 39.1 | 65.6 | 68.2 |
| w4 17:13-17:16(与 W09 同时刻) | /markets?limit=1 | 200 | 35.5 | 38.5 | 42.9 | 46.0 | 46.4 |
| **合并 w1-w3** | /exchange/status | 597 | 31.2 | **34.7** | 38.2 | **44.5** | 94.7 |
| **合并 w1-w3** | /markets?limit=1 | 600 | 34.2 | **38.7** | 43.1 | **72.5** | 114.0 |

(单位 ms。总样本 1,200,热 200 响应 1,197 条,错误 0。冷连接
(TCP+TLS 首包)三次:148/152/150ms。/markets 比 /exchange/status 稳定
慢 ~4ms 且尾部更肥——服务端查询成本不同,POST 下单落在哪一侧未知,
这正是 §4 取最差端点的理由。)

三角验证:C++ libcurl 热 lane(`bench_rtt` n=30,15:35Z)p50=33.5ms、
p99=36.3ms——与 Python 探针差 <1ms,探针自身开销可忽略;历史多日
网络层基线(work/latency_baseline/samples.ndjson,2077 条,07-09 起)
热 p50≈31ms 同量级。**时段覆盖局限如实声明:**本次三窗口跨约 2 小时
(15:24-17:04Z),未覆盖 UTC 0-3 高峰;多日跨时段的网络层基线显示
p50 漂移 <3ms,视为可接受旁证,后续可用同脚本补高峰窗口。

## 2b. Tier 3 — 测点对比:Mac vs W09 EC2(同一脚本,同一时刻)

操作员 2026-07-16 开机 W09 解除阻塞后执行。测点:W09
(i-0e53d134dceffe166,us-east-2,ip-172-31-47-117)→ 生产,
`tools/latency_probe_auth.py --throwaway`(W09 隔离契约禁真凭据;一次性
本地 RSA key,公共端点忽略签名——与 bench_rtt 同一先例;ndjson 带
key_source 字段防混数)。窗口 17:12-17:21Z,n=1,000,0 错误;Mac 侧
w4 窗口(带真鉴权)与其同时刻运行,隔离测点差异与时段差异。
原始数据:`work/latency_baseline/tier3_w09.ndjson`。
盒上临时 key/文件已删;空闲 30 分钟自动停机守卫确认 active。

| 测点(同时刻) | 端点 | n | p50 | p90 | p99 | max | 冷连接 |
|---|---|---|---|---|---|---|---|
| **W09 us-east-2** | /exchange/status | 499 | **6.2** | 6.9 | **9.4** | 12.7 | 13.6ms |
| **W09 us-east-2** | /markets?limit=1 | 500 | **8.0** | 9.4 | **14.5** | 24.9 | — |
| Mac 住宅网络 | /exchange/status | 199 | 34.5 | 39.1 | 65.6 | 68.2 | 149ms |
| Mac 住宅网络 | /markets?limit=1 | 200 | 38.5 | 42.9 | 46.0 | 46.4 | — |

(单位 ms。)结论:**同区域云机白捡 ~28ms(p50 34.5→6.2),p99 从
44-73ms 压到 9-15ms,冷连接 149→14ms。**W09 在 us-east-2,Kalshi 源站
在 us-east-1;若执行机真放 us-east-1,还有余量(bench_rtt 的 sub-5ms
判据、生产盒 catalog_sync 反推 <1ms 均指向此)。

## 3. UNMEASURED_CONSERVATIVE 记账(config/backtest_latency.yaml)

| 参数 | 状态 | 值与来源 |
|---|---|---|
| signing_p99_us | **MEASURED** | 500(实测 452.8 向上取整;本文件 §1) |
| processing_chain_p99_us | UNMEASURED_CONSERVATIVE | 2000(decode+apply 实测 282ns/msg,但"WS收→策略看见→意图成形"全链未实测;保留 2ms 悲观余量) |
| signed_post_rtt_p99_us | UNMEASURED_CONSERVATIVE | **91000**(= 合并最差端点 /markets p99 72.5ms × 1.25;§4 规则。注意:旧占位值 60000 被实测证明偏乐观,已纠偏) |
| 交易所下单处理耗时 | UNMEASURED | 只能 Tier 4(生产微基准,默认锁死,操作员另批)实测;在 signed_post_rtt 的余量里吸收 |

## 4. signed_post_rtt 的推导规则(写死,防止未来拍脑袋)

signed POST(下单/撤单)与本次实测的 signed GET 差 = 服务端撮合处理 +
请求体传输(~300B,量级 µs)。规则:
`signed_post_rtt_p99_us = ceil(Tier 2a 全窗口最差端点 p99) × 1.25 余量`,
测点=实际部署机。当前部署机未定 ⇒ 用 Mac 数字(比 EC2 悲观,方向安全,
符合 Q2)。Tier 4 实测后整段替换。

## 5. 受阻项裁决记录(E2)

- **Tier 2b(demo 下单/撤单正确性排练)= BLOCKED,操作员维持裁决
  2026-07-16:不建 demo 支持,正确性验证并入将来的 Tier 4(生产微基准,
  另批)。** 原始受阻原因存档:(a) `~/.kalshi/env.sh` 只有生产凭据,无
  demo key;(b) 引擎安全层按设计拒绝 demo:`KALSHI_ENV=demo` fail-closed
  (src/env.cpp:163-166,kalshi_facts.yaml `engine_gate`,修正案 A2)。
  安全层保持原样,未动。
- **Tier 3 EC2 侧曾于 15:0xZ 受阻**(W09 停机、Mac 无控制面、生产采集机
  禁碰),操作员当日开机 W09 后解除,实测结果见 §2b。全程未触碰生产
  采集机;W09 上未放置任何真实凭据(--throwaway 模式)。

## 6. 部署含义(给策略/架构,不是决定)

实测定案:Mac 住宅网络热 p50 ≈ 34ms;同一时刻 W09(us-east-2)
p50 = 6.2ms、p99 = 9.4ms(status)。**执行机放云端 = 白捡 ~28ms**,
对"防守速度(tick-to-cancel)是生死线"的做市主线接近必然;us-east-1
(Kalshi 源站区域)预计还能再降。部署裁决与预算是操作员决定;若裁决
云端部署,signed_post_rtt_p99_us 届时按 §4 规则以部署机重测替换
(W09 数字预演:14.5ms × 1.25 ≈ 18.1ms → 19000µs,仅供预估,未入账)。

## 7. 2026-07-21 增补 — 尺子回主线 + 热路径回归门(ACCEPTANCE 第10条门)基线

**背景**:本文件与 `bench_engine` 此前只活在未合并分支
`plan-sports-market-dynamics-v2`(commit 294d7fe),主线上"丢失"。2026-07-21
按操作员直令恢复进主线(apps/bench_engine.cpp + apps/bench_engine_core.hpp +
tests/test_engine_bench.cpp 挂回 `make check`,Makefile/CMake/tools.json 三处同步),
并新增一个指标:

- **ring_handoff(决策→交接)**:tradingd 形状的 OrderMsg 过 Vyukov 环、消费端
  spin+yield(镜像 submit_worker spin 分支)、乒乓步调(空环常态)。

**复测封存(Mac,RitcardodeMacBook-Pro.local,throwaway RSA-2048,n=10000×3 轮,
2026-07-21T22Z,原始行均已追加进 engine_bench.ndjson):**

| 指标 | p50(3轮) | p99(3轮) | 与 07-16 封存值比 |
|---|---|---|---|
| sign_request | 283.7 / 286.0 / 284.0µs | 446.4 / 484.0 / 447.4µs | p50 282.6 / p99 452.8 → **吻合,基线连续** ✓ |
| chain_build_to_signed | ~284µs | 306–424µs | 一致 |
| **ring_handoff** | **0.1–0.2µs** | 0.2–0.3µs | 新指标,首次封存 |

**更正**:此前口头流传的"决策→交接 p50 ~7µs"在仓库里**没有任何落盘工件,作废**。
纯环交接实测 ≈0.15µs;若 7µs 为真,多半含 bell 唤醒/调度,属实盘口径——待
shadow 模式 + full_chain probe 实测后另行入册。在那之前,第10条门的"决策→交接"
以本节 ring_handoff 为准。

**10.7 回归比较规程(操作员已批准 2026-07-21)**:同主机、同工具、同密钥位数;
改动前后各跑 3 轮 n=10000,各指标取 3 轮 p50/p99 的中位数比;任一中位数变差 >10%
= 回归 = 不过。所有原始行追加进 `work/latency_baseline/engine_bench.ndjson`,
只追加、不删挑。

**开口项**:生产在 EC2——若第10条门在 EC2 上裁,先在该机跑一次 bench_engine
封存 EC2 基线(§6 的部署含义不变)。

## 11. Tier 4 — 生产实盘下单/撤单首测(2026-07-25,历史第一批实盘订单)

测点:EC2 prod(us-east-2)→ `external-api.kalshi.com`,warm lane。
工具:`build/bench_order`(post-only 1¢ YES 买单,构造上不可成交,同轮撤销)。
标的:KXBTC15M-26JUL250200-00。n=5(管道验证轮)。操作员亲手执行(`!` 通道)。
原始日志:EC2 `/tmp/bench_prod_n5_*.log` → 归档 `work/latency/receipts/`(哈希待录)。

| 环节 | p50 | min | max | p99 |
|---|---|---|---|---|
| RSA-PSS 签名(EC2) | 0.9ms | 0.9ms | 0.9ms | UNMEASURED(n=5) |
| place wire→ack | 4.6ms | 4.2ms | 5.2ms | UNMEASURED |
| **cancel wire→ack** | **3.8ms** | 3.6ms | 4.8ms | UNMEASURED |
| 决策→order-ack 全链 | 5.5ms | — | — | UNMEASURED |

**独立对证(交易所账本,签名 GET /portfolio/orders,2026-07-25T05:51:06Z):**
5 单全部 canceled;服务器侧 created→canceled = 3.61/3.81/3.95/4.15/4.77ms,
与 bench 的 cancel p50 3.8ms 咬合;相邻创建间隔 8.7–10.8ms ≈ 一轮
sign+place+cancel(0.9+4.6+3.8≈9.3ms)。两套独立时钟互证,数字为真。

**2026-07-25 全套补测(操作员授权实盘探测单;全部收据在 EC2
`work/latency/receipts/` 带 sha256):**

| 项 | 实测 | 备注 |
|---|---|---|
| 时钟(0.1) | chrony 偏移 <1µs(AWS Time Sync) | 单向测量地基成立 |
| n=50 下单 | p50 4.1 / max(≈p98) 5.7ms,50/50/0 | 双侧活市场 36/37¢ |
| n=50 撤单 | p50 4.1 / max(≈p98) 6.3ms | 保守记账上界收紧至 **≤7ms** |
| **并发撤 10 张**(3.4/5.1 核心) | 单张 3.7–5.1ms,**全撤墙钟 5.0ms** | 10 条预热连接并行,无串行化惩罚 |
| 冷连接罚(G) | TCP 1.3–2.1ms + TLS ~20ms ≈ 25ms | 实盘引擎必须连接池常温 |
| RTT 地板 | ~1.5ms | 物理对质:4–6ms ack > 地板 ✓ |
| 限速(C,官方账户接口) | **advanced 档:读写各 300 tokens/s、桶 600;create=10、cancel=2 tokens** | 撤退燃爆 300 张/瞬,持续 150/s——**非瓶颈**;**下单持续 30/s、burst 60——报价刷新的硬预算,入 E4** |
| ticker 频道入站滞后(D) | p50 0.51s / p90 1.5s(n=3,869) | 坐实:ticker 不能当警报器;警报=L2 delta/BRTI/Coinbase |
| REST cfbenchmarks 成本 | 50 tokens/次 | BRTI 只能走 WS 频道 |

**未测余项:** fill 推送(4.1,需真实成交,操作员亲手);自单可见(4.2,
因 L2 订阅窗口缺口未测成,可随时补);BRTI/Coinbase 入站(1.2/1.3,等部署);
EC2 签名尾部(低优先);风暴日全链彩排(5.1 完整版)。

**数据质量警报(2026-07-25):L2 采集订阅滞后于 15 分钟窗口轮换**——hour-15
文件仅含 11:15 窗口(34.5 万条),11:30/11:45 窗口整窗缺失。12 天 BTC15M L2
为逐窗口有洞;E4 必须逐窗口报告覆盖率;已通报 build 线修订阅刷新。
