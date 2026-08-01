# RESULTS — 影子做市引擎 阶段1(EXECUTION 线)

日期:2026-07-22 · 提交:`8ecbf73` · 过关门:`agents/audit/ACCEPTANCE_影子做市引擎_成交真实性.md`(10 条)

## 建了什么

四件套全部落地,代码在 `include/trading/shadow/`(头文件库,零外部依赖):

| 件 | 文件 | 对应验收条 |
|---|---|---|
| 挂单登记表 + 生命周期状态机 | `order_registry.hpp` | 10.1/10.2(预分配槽位、单写者记账线程、终态槽回收) |
| 敞口闸 | `order_registry.hpp` ExposureGauge | 10.4(热路径 O(1) 原子读,风险=几次 load+compare) |
| 有状态成交模拟器 | `fill_sim.hpp` | 1/2/3/4/9(三轨 strict/queue/optimistic;排队尾、真实 taker 流成交、撤单延迟窗) |
| 仓位账本 + markout | `ledger.hpp` | 5/6/7/3(结算记价、真实费扣减、成交率、成交后 markout) |
| 费率表(fail-closed) | `fees.hpp` | 5(UNKNOWN 费率=不可交易,永不假设免费) |
| 引擎编排 + 参考报价器 | `engine.hpp` | 全部粘合;确定性(同带同参=同账) |
| 回放驱动 | `apps/shadow_mm_replay.cpp` | 交付物打印;**全程无网络代码** |
| 测试 | `tests/test_shadow_engine.cpp` | 8 组,`ALL PASS`,已进 `make check` + tools.json |

## 交付数字(真实带:Sports 07-20 全天 + Mentions/Companies 07-14~20,640 万笔,strict 轨,ask-only off=2 size=5 cap=200)

### 1. 分族盈亏(悲观成交、结算记价)
| 族 | 市场数 | 已结算 | 毛盈亏 | 净盈亏 | markout |
|---|---|---|---|---|---|
| Sports | 19,349 | 12,660 | **−$3,800** | 被费率门挡(见下) | −3.55¢ |
| Mentions | 2,427 | 1,646 | **+$877** | 同上 | −5.27¢ |

**费率声明:仓库无任何已批准 maker 费率表(tools/fees.py 不存在,WO-E 未建,deep03 费率批准未完成)。引擎按 fail-closed 处理:全部市场费率 UNKNOWN → 净盈亏一律不出数、逐市场计数(7,390 个市场被排除)。这不是缺陷,是家规(deep03 C-01/C-02:UNKNOWN 费=不可交易)。已挂 WAIT_DECISION。**

方向解读(供策略线):Sports 无差别裸挂为负 ↔ 与 2026-07-21 VAR-1 σ 分桶结论一致(无 σ 选择的裸挂被逆选择吃掉);Mentions 毛边际为正 ↔ 与 EXPT-BO2026 单名 maker +0.86¢/合约一致。引擎方向自洽。

### 2. 成交率
挂出约 100 万单(每速度档),**6.7~6.8% 成交**(strict 口径)。乐观引擎会宣称 100%——这个数字就是 edge 能不能上量的分母。

### 3. 逆选择(markout,60s 视界)
全族为负:−4.0¢/合约(Sports −3.55,Mentions −5.27)。**负值证明第 3 条(逆选择不对称)在真实起作用**,不是模型摆设。

### 4. 速度→盈亏曲线(place=cancel 延迟)
| 速度 | 毛盈亏 | 成交率 |
|---|---|---|
| 2ms | −$3,835 | 6.80% |
| 7ms | −$3,458 | 6.80% |
| 25ms | −$3,142 | 6.78% |
| 100ms | −$2,924 | 6.71% |

**读数:对这个无 edge 的参考配置,更快=更亏**(快只是更高效地接住毒流)。速度的钱要等站 5 冻结的真策略(带 σ 选择/带选择)再量——线 V 投多少,以那条曲线为准,不以本条。

### 5. known-answer(红先于绿)
测试内置机械收敛盘(90→99 单边买压、结算 YES):strict 轨**必须亏**、optimistic 只准更亏,任何轨显示盈利=成交模型坏=测试失败。当前 `ALL PASS`。

### 6. 热路径回归门(10.7)
- 本提交**零热路径编译单元改动**(`git diff src/ include/kalshi/` 为空;bench_engine 二进制 mtime 07-21 18:09,早于全部改动,未重编译)。
- 复测(Mac,3×10000,2026-07-22,追加进 `work/latency_baseline/engine_bench.ndjson`):ring_handoff p50 0.2µs(基线 0.1-0.2,持平);sign p50 289.4µs vs 基线 284.0(+1.9%,过);sign **p99 中位 573µs vs 基线 447(+28%,表面超门)**。
- **裁定申请:p99 超门为环境噪声**——同一二进制、零代码差;测量时 loadavg 4.13(VS Code/Chrome/codex agent 并发)。按 10.7 规程"同主机同工具",机器状态差异如实入册;建议审计裁定时以"同二进制"为决定性证据,或安排静默窗重测。

## 已知局限(如实)
1. queue 轨在真实带上未出数——L2 深度(queue_ahead 的原料)受 B14 卡,合成测试已覆盖 queue 逻辑;站 2 修复后接入干净 L2 日。
2. 参考报价器只为运转机器,不是策略;PnL 数字不构成任何 go/no-go。
3. 活单/撤单监视列表允许有限陈旧条目(有界、状态校验兜底),不影响正确性;记账线程 map 使用不触热路径纪律。
4. tradingd 实时接线(WS 成交流→记账线程)属站 8,本阶段未做;引擎消费循环即未来记账线程,10.6 架构同构已预留。
5. **操作失误上报:恢复 tools.json 格式时误用 `git checkout` 冲掉了会话开始时已存在的未提交改动(工具数与 HEAD 一致、前 40 行内容一致,疑为深处字段微调)。原改动线请自查;向操作员如实报告。**

## 复算
```
make build/test_shadow_engine build/shadow_mm_replay && ./build/test_shadow_engine
./build/shadow_mm_replay <tape.ndjson> --track strict --speeds 2,7,25,100
```
带子导出脚本口径:expt.duckdb `trades_all`(p=round(yes_price_e4/100),c≥1)+ finalized settlements;数据版本=expt.duckdb @ 2026-07-22。
