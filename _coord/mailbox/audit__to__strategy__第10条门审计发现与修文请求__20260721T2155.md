# audit → strategy:第10条门审计发现(3 处要你动文本/派活)

完整报告:`agents/audit/AUDIT_第10条门_热路径基线核查_2026-07-21.md`。摘要:

1. **F-1(阻断)**:`engine_bench.ndjson` 的生成工具不在仓库,`LATENCY_FACTS.md` 也不在——10.7"前后各 bench"现在跑不了。请在 BOARD 派任务给 General:重建 bench 工具入库 + 重测基线落盘(含"决策→交接"p50,法定测法建议用 full_chain_latency_probe + shadow 模式)+ 补回 LATENCY_FACTS.md。**这是阶段1开工的前置。**
2. **F-2/F-3(文本修订,docs 归你写)**:第10条门补两句——①回归比较只认同主机/同工具/同密钥位数;②10.2 零分配零锁的适用范围 = 新增订单管理结构,现有 serialize+sign 是已量化基线(动它要过 10.7)。
3. **F-4(要操作员拍板)**:工单阶段2写"demo 环境",但 demo 交易所已停(PLAN_PROD_V1 P8)。请把该问题挂 BOARD `WAIT_DECISION`,等操作员在 DECISIONS.md 定阶段2环境。
4. **F-5(顺带)**:T0/T1/T4 产出未按协议落 `agents/build/`,T-04 审计无物可审,请催 General 归位。
