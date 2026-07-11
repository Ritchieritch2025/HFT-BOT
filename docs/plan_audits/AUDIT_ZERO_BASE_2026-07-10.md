# AUDIT — 零基审计(zero-base)2026-07-10

**状态 = 审计意见,未裁决;GUARDRAILS 修订与主线切换等操作员批准。**

归档说明(非审计原文):以下为操作员 2026-07-10 粘贴的零基审计全文,逐字
保存(CLAUDE.md 全文保存规则)。本文件内的任何"裁决/推翻/执行顺序"均为
**审计意见**,在操作员正式批准前不改变 GUARDRAILS、MASTER_SEQUENCE 或任何
现行计划的效力;归档会话未执行其中任何建议。

---

❌ 严格审计结论:现有总路线不应继续照单执行。安全工程大体正确,但盈利主线选错了优先级。
当前最合理的策略裁决是:
第一条可验证赚钱路线:MLB 赛前 totals/spreads 的外部 sharp-odds 锚定、选择性 post-only 做市。
最高上限的终局路线:RFQ/combo 做市,用外部赔率定价并用单腿市场管理风险。
同时运行体育合约逻辑约束套利扫描器,但不把非原子多腿成交冒充无风险套利。
本轮是只读审计,没有继续修改当前脏工作树。
一、为什么现有主线不通过

朴素盘口做市已经被自己的数据否定
S1 的悲观口径:
ITF:−0.822¢/fill,95% CI 全负。
Challenger:−1.348¢/fill,95% CI 全负。
ATP/WTA 样本不足,但没有正面证据。
来源:results.json。
这推翻了"maker零费 + 收价差自然赚钱"。taker fee 并不保护 maker;它可能把噪声交易过滤掉,留下更有信息、也更毒的成交。
当前所谓 bracket arbitrage 不能算套利
现有报告承认:
mutually_exclusive 只证明至多一个结果发生,不证明至少一个发生;
catalog 可能漏腿;
quote 可以陈旧一小时;
没有深度和多腿成交模型;
多腿 taker 费用后,p50 已经约为 −0.8¢。
来源:gate_report_2026-07-07.md (line 64)。
因此2629个"信号"只是上界,不是2629次套利机会。这个报告应从策略证据降级为"错误检测器"。
当前回测不能判利润
旧回测器仍然:
写着 maker fee=0;
没有 settlement;
requote 时旧订单瞬间消失;
每次成交固定填满;
没有 cancel-pending、partial fill、queue calibration。
来源:mm_backtest.py (line 4)。
候选扫描器也只是 spread × trade_count,不去重、不扣费用、不看毒性、不看真实成交率:mm_scan.py (line 1)。
当前执行引擎不具备安全实盘资格
tradingd 仍由 REST poll 驱动,不是 WS full-depth:tradingd.cpp (line 619)。
更严重的是,它发单时调用默认 post_only=false 的订单构造,并没有把已经完成的五层风险账本、rule engine、reconcile 接入下单路径:tradingd.cpp (line 291)。
所以 W-K1..K5 "组件完成"不等于生命周期安全门完成。
我们没有足够的体育成交真相
本地数据审计:
Sports:约670万 unique trades,覆盖约2.1万 markets。
L1:约2,856万行、4.4万 markets。
L2:仅103,252行、4个 Sports markets,约30分钟样本。
没有同步 sportsbook odds、point/game/set、发球方或可靠 in-play score timing。
因此可以做市场普查,但还不能证明 queue、fill 或外部锚领先性。
S1 的 burstiness 有实现错误
函数声称计算 book inter-update,实际上从交易分析表 a 计算时间间隔:aggregate_maker_edge.py (line 358)。
所以现有"赛中 p50=4ms"不是盘口更新速度,不能用于系统反应判决。
二、现有条款的推翻/保留裁决
条款/计划 裁决
S1–S6 安全、不盲重试、kill switch、fixed-point 保留
Q1"一切 strategy math 都必须在 log-odds" 重写。预测残差、vol、skew 可用 log-odds;费用、PnL、完整集求和、支配关系必须在 payout/price space
Q2"strict-through 是唯一 go/no-go" 重写。它保留为最坏诊断;最终 gate 应是经真实 queue probe 校准的保守模拟下界
Q7 一刀切排除 MVE/combo 推翻。通用 CLOB 引擎仍禁止误碰 MVE,但应建立独立 RFQ/combo strategy adapter
H1"taker费率墙保护maker" 推翻为待检假设;S1 当前证据反对它
全18类别 full-market L2 降级。保留全市场L1/trades归档;关键路径只做全体育 L2 + 对照样本
通用 Avellaneda–Stoikov 定价 降为 quote-shaping 辅助,不再是 fair-value 主体
7 clean days、5/7天、25%集中度、×1.5/×2 暂停作为硬门。这些数字未由功效分析或风险预算导出
shadow 5绿日后才做queue probe 顺序错误。没有真实 probe,shadow fill 仍是自我验证
PLAN_LIVE_VALIDATION 撤回重写;包含手动平仓、demo错误和真实订单提前执行问题
全市场研究计划 保留为背景 atlas,不占体育策略关键路径
Dashboard、历史迁移、Gold、旧审计计划 作为历史证据归档,不再控制当前策略顺序
SESSION_LOG、访谈档案 保留历史,不作为当前事实权威
特别是 GUARDRAILS.md (line 54) 的 Q1/Q2/Q7 应正式重写;MASTER_SEQUENCE.md 应停止作为当前策略队列。
三、Kalshi 体育市场真正值得利用的结构
第一层:外部赔率锚定的选择性 CLOB maker
不是机械 join touch,而是:
edge_lower
= 外部去水公允价与我方报价的方向性差


maker fee
锚延迟/映射误差
adverse-selection 保守项
cancel race
库存退出成本
只有 edge_lower > 0 才挂单。
首个 pilot 应选 MLB 赛前 totals/spreads,原因:
本地样本中 Baseball 零 maker-fee 衍生盘约8,000万 contracts;
标准化程度高,外部 sportsbook 覆盖好;
每日重复,跨 regime 验证比 World Cup 更快;
totals/spreads 多数是 zero-maker-fee,而 MLB moneyline 主系列收费;
比 ITF/Challenger 更容易获得可靠 sharp reference。
Soccer 零费流量更大,但当前样本是 World Cup 特殊 regime;Tennis 零费流量也很大,但 S1 已显示朴素报价显著负。
第二层:RFQ/combo maker——最高上限
Kalshi 官方 RFQ 有几个关键优势:
RFQ 可用于普通市场和 combo;
maker 看见 ticker 和 size 后再决定是否报价;
竞争者报价彼此私有;
combo 属于 HVM,确认窗3秒、执行窗1秒;
不确定时可以拒绝一侧或不报价;
quote API 支持 post-only。
来源:RFQ机制、Create Quote。
这绕开了公开 CLOB 的队列竞争,更适合我们建立:
外部赔率定价;
规则过滤;
event/factor 风险聚合;
只接"价格足够好"的散户 combo flow。
第一阶段只做跨比赛、标准 moneyline/total legs;拒绝同场高相关 parlay、player props 和可能 DNP scalar settlement 的腿。
第三层:体育合约约束套利
构建每个 event 的 payout matrix,用 LP/约束求解检测:
完整结果集 Σprice ≠ 1;
事件包含关系违反 P(A) ≤ P(B);
moneyline、spread、total、BTTS、correct score 之间的逻辑矛盾;
tournament advance/futures 的层级矛盾。
但 batch API 返回逐单结果,不能假设多腿原子成交。因此只有在:
锁定收益 > 全部费用 + 滑点 + orphan-leg最坏损失
时才可执行;否则只是报警。官方 batch response 的逐单 error 结构说明 partial failure 必须建模:Batch Create。
四、需要引入的外部工具
推荐按顺序:
OpticOdds trial:最快获得多 sportsbook 实时 SSE、历史赔率和比分,先做覆盖/延迟 bakeoff。官方文档
官方 Pinnacle API 申请:sharp anchor 最理想,但不能假设一定获批。官方申请说明
Betfair Exchange Stream:如果账户与所在地合法可用,既可作为 sharp reference,也可能作为外部 hedge venue。官方文档
Sportradar 30日 trial:只在准备研究 in-play 时引入;先证明 score feed 比 Kalshi 有领先价值。官方文档
HiGHS/OR-Tools:用于 payout-matrix 和跨合约无套利 LP。
申请 Kalshi Market Maker Program:指定 MM 可能获得降费和调整后的限额;这会直接改变策略经济性。官方说明
捕获市场级 liquidity incentives,但只有实际到账才计入 PnL。官方计划
任何付费订阅、账户申请或外部交易场所开户仍需你确认;本轮没有代你购买或发送申请。
五、建议的新执行顺序
在正常终端先 checkpoint 当前未提交 Markdown,但标成 DRAFT/REJECTED-PENDING-REWRITE,不能当已批准计划。
建立唯一 CURRENT_AUTHORITY.md;历史计划全部降为 evidence。
立即增加只读 communications RFQ capture,统计市场、size、频率和生命周期。
只扩 Sports L2:MLB、Soccer、Tennis、Basketball,加少量长尾对照;停止全18类L2关键路径。
同时跑 OpticOdds/Pinnacle/Betfair 数据源 bakeoff:fixture mapping覆盖率;
更新时间;
P(anchor move领先Kalshi > cancel_effective);
历史深度、成本和断流。
建 payout graph + LP 套利扫描器。
用 MLB 赛前 totals/spreads 做第一轮外部锚 maker 回测。
把 RiskLedger、RuleEngine、reconcile、post-only、cancel-on-pause、order groups、private WS 真正接入 tradingd。
先完成 W-K6,再做极小 queue/RFQ quote probe,校准真实 fill/win curve。
校准后重新冻结 simulator,再做 validation/test/shadow;最后才微实盘。
最终裁决:
停止把"通用盘口做市器"当产品。产品应改成"Sports Pricing & Constraint Engine":外部 sharp odds 给 fair、合约逻辑图防错/找套利、CLOB 与 RFQ 只是两个执行出口。
当前所有 live 行为继续禁止:测试未全绿、计划未提交、fee gate 未正式 ratify、L2/外部锚/RFQ 数据缺失,而且 live engine 尚未满足自身安全条款。
