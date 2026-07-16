# 回测系统缺口地图(设计备忘,2026-07-16)

**状态:PLAN-ONLY 构思文档。** 不授权研究执行、生产变更或任何实盘行为。
上位文档:`docs/PLAN_MM_TEST_PROGRAM.md`(总测试计划,含 W-FS1 规格与五乘数
分解)。本文件只做三件事:盘点已有资产、对齐 deep03 新策略候选的需求、
列出按优先级排序的建造清单。

## 1. 一行裁决

**回测系统的架构已由 PLAN_MM_TEST_PROGRAM 定案(三轨成交 + 唯一状态机 +
五乘数分解),沙盒引擎 mm_sandbox.py 已实现大半;真正缺的是三块:
receive-clock tape 适配器、新策略插件、queue 校准数据。**

## 2. 已有资产(不重复建设)

| 资产 | 位置 | 状态 |
|---|---|---|
| 总测试蓝图:研究→影子→微实盘,G 门体系 | docs/PLAN_MM_TEST_PROGRAM.md | PLAN 批准,F2c/F2d/FA-1 增补各自审计 PASS |
| 五乘数分解(报喜必须带全套) | 同上 §1 | 判决语义生效 |
| W-FS1 成交模拟器规格:FS-1 唯一状态机 / FS-2 三轨成交 / FS-3 实测延迟分布 | 同上 §8 | 规格冻结,未完成 |
| 沙盒引擎:订单生命周期(submit→resting→cancel_pending→terminal)、strict-through/queue/optimistic 三轨、三个基准策略 | sandbox/research/mm_sandbox.py | 可运行;等 receive-clock tape 适配器(Gold 时代输入已判 DIAGNOSTIC_ONLY) |
| 旧上下界诊断回测 | tools/mm_backtest.py | 永久限定诊断级,无 go/no-go 资格 |
| 定价数学(log-odds/E4/费用) | tools/pricing/lo.py(W-P1..P4 已审计) | 可直接复用 |
| 离线风控/kill switch/reconcile | W-K1..K5 | 已完成并审计 |
| 时间戳阶梯 + jitter 工具 | W-TL1 | 已落地 |
| 研究治理方法论(root-event 聚类、day-block bootstrap、负控制、预注册) | deep01/deep02 框架 | 已实战两轮 |
| 延迟参数文件 | config/backtest_latency.yaml | 存在但参数未实测 |

## 3. deep03 新策略候选对回测的新增需求

(候选定义见 docs/research_reports/DEEP03_SPEC_NOTES.md)

| 候选 | 对引擎的新要求 |
|---|---|
| 拉锯区双边报价(88–93) | ① L2 深度蒸发信号作为策略可见输入(kill 触发);② 强平路径模拟:cancel-pending 暴露期 + taker 费 + 按实测盘口深度走的滑点;③ 按联赛分层输出 |
| 卖确定性(97–99 阶梯) | ① 结算路径 PnL(持有到结算的口径,非仅 markout);② 单笔亏损封顶的账本语义 |
| 驻军指数选品 | 离线预计算的每市场特征表,作为策略输入而非引擎内建 |
| 失明成本量化 | 撤退信号提前量/误报率的回放统计(引擎的事件流上直接可算) |

## 4. 缺口清单(按建造顺序)

1. **G-1 tape 适配器(第一优先,现在就能做):** sealed release(L1 parquet +
   trades csv.gz + L2 parquet)→ 按 local_recv_ts_us 归并的确定性事件流,
   喂 mm_sandbox。含:gap 区间剔除、E4 直通、逐系列 fee_type 查找、
   输入指纹回执(沿用 deep02 三回执模式)。**不需要等 20 天数据**,
   两个正典日就能建成并端到端冒烟。
2. **G-2 策略插件接口收敛:** 单一抽象(on_event → desired quotes),
   同一实现将来直接接影子测试的实时流(FS-1"同流"要求,防回测/实盘漂移)。
   先移植两个候选:拉锯区双边(带 retreat-kill)、卖确定性阶梯。
3. **G-3 queue-aware 轨校准:** queue-ahead 起点 = 挂单时该价位显示量,
   消耗 ≤ 公开成交量(FS-2 中轨)。依赖连续 L2 天数(每天在积累)。
4. **G-4 延迟分布:** backtest_latency.yaml 实测前,以敏感性矩阵替代
   (如 5/50/500ms 三档全策略重跑,结论须在三档下同号才算稳);
   实测分布留给微实盘阶段的 order_latency_test。
5. **G-5 报表:** 五乘数分解 + 悲观轨 go/no-go + 按联赛/日分层 +
   delete-best-subgroup 稳健性,全部沿用既定判决语义。

## 5. 阶段划分(全部 paper-only)

- **Phase A(数据等待期,现在):** G-1 + G-2,用两个正典日端到端跑通,
  产出 DIAGNOSTIC_ONLY 的五乘数分解表(目的:管道贯通 + 参数敏感性感受,
  不出任何 edge 结论——样本不够 + PRIOR_EXPOSED)。
- **Phase B(≥20 独立日 + 费率 ratify 后):** deep03 正式预注册,
  悲观轨作 go/no-go 裁决。
- **Phase C:** 同一策略类接实时流做影子(PLAN_MM_TEST_PROGRAM 既定门槛,
  shadow 5 绿日),再往后是 operator-gated 微实盘。

## 6. 治理边界

- 本文档不改变任何授权状态;Phase A 的实际动工需操作员一句确认
  (研究执行权限,D-1 语义)。
- 引擎建设属工程,策略结论属研究:Phase A 产物一律 DIAGNOSTIC_ONLY 横幅。
- W-FS1 完成前,任何"正收益"无 go/no-go 资格(总计划 §判决语义 3,重申)。
