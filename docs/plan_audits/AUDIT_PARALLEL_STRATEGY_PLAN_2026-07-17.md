# AUDIT — 对"并行推进基建+策略、争取本周最基础策略上线"计划的再审计

日期:2026-07-17 · 审计者:本会话(零改动,只读核查)
被审计对象:操作员粘贴的上一会话审计报告(原文见本文末尾附录,逐字保存)
操作员本轮目标声明:「在完善基建的同时并行开发策略,这样这周之内最基础的
策略就可以开始上线了;目前 Research data input pipeline 还在完善」

## 一行裁决

⚠️ 该报告方向正确、四线并行建议采纳、"live 保持关闭"正确;但
**"本周上线"只有"影子上线"(不动真钱)一种合宪解释**;报告本身有
1 处数字硬伤、1 处对现有安全闸门的失实描述、2 处未经裁决就当作已定的
门槛改动(需操作员补 D-5/D-6 裁决才生效)。

## 逐条核查(每条给出仓库内证据)

| # | 报告声称 | 核查 | 证据 |
|---|---|---|---|
| 1 | 体育 match 系列有 maker 费,旧"maker 0 费护城河"结论要重算 | ✅ 属实 | config/kalshi_facts.yaml(dirty diff):KXATPMATCH/KXWTAMATCH/KXNBA = quadratic_with_maker_fees(07-16 实测);错误结论在 docs/research_reports/DEEP03_SPEC_NOTES.md:24;注意 KXATP(锦标赛冠军)= quadratic 无 maker 费——必须逐系列查,不能一句"体育有/无 maker 费" |
| 2 | 费用修订还在 dirty tree,V3 前须干净 commit | ✅ 属实 | git status:kalshi_facts.yaml、PLAN_SPORTS_TRADING_DECISIONS.md(含 D-4 裁决本体!)、4 个哨兵测试全部未提交 |
| 3 | PLAN_MM_TEST_PROGRAM 有"7 日、5/7 正收益"条款 | ✅ 属实 | F4 硬门第 2 条(≈:554)"至少 7 个完整交易日;test 中正 PnL 天数 ≥5/7" |
| 4 | 建议把 5/7 改成 root-event 样本量门 | ⚠️ 未裁决不生效 | D-1(2026-07-10)明文驳回"暂停既有数值门":"保守默认值在被功效分析替换前继续有效,标注 provisional"。改 F4 需要操作员新裁决(D-6)或功效分析,报告称"按你本轮裁决"但台账无此条目——按 E2,不存在 |
| 5 | "不需要等 20 天启动 V3" | ⚠️ 半对半误导 | 07-15 授权书 §4 双档制早已裁决:探索档**现在就不停机**,≥20 独立日是 PROMOTION_READY 判决档的**冻结门槛("维持冻结门槛,不变")**。即 deep03 启动从未被 20 天挡住;报告真正提议的是**让候选不满 20 日也能晋级进 shadow**——这是在改一条操作员冻结的门,需 D-5 裁决 |
| 6 | future-event holdout 立即冻结 | ✅ 方法论成立 | 07-12/13(deep01 用过)、07-15(DEEP03_SPEC_NOTES:33 承认已看)确已全部暴露;冻结未来 event 为考卷、同 root-event 不跨 split、冻结前不读 holdout PnL——零等待成本开始积累干净考卷,建议**作为新增机制批准**;它是否**替代** 20 日晋级门,单独裁决 |
| 7 | tradingd"直接签名发送,没有闸门" | ❌ 失实(夸大) | apps/tradingd.cpp:283-289:`orders_enabled=false` 时一切订单强制转 Shadow 记录、永不发送,注释明言"NEVER transmit…the gate is here so it can never regress"。**缺的东西是真的**:无 RiskLedger/OrderManager/reconcile 接线、OrderIntent.post_only 默认 false(include/trading/bus.hpp:170)、无持久化订单状态机——工程闭环清单本身采纳 |
| 8 | 默认 roster 只有 once_probe | ✅ 属实 | src/strategies.cpp:13-21(且不在默认 roster,STRATEGIES 不点名则零订单) |
| 9 | make check 用 tail -1 无 pipefail,可掩盖失败 | ✅ 属实,高优先小修 | Makefile:376-377:`$$t $(SCRATCH) \| tail -1`——管道退出码取 tail 的 0,测试二进制非零退出不会让 check 变红,违反 E1"绿灯不说谎"(D2)。一行修复(pipefail 或显式检查退出码) |
| 10 | readiness 把刚过的测试判 not_started | ✅ 属实 | tools/lifecycle_check.py:305-308:latest.json 无记录即 not_started——是"收据未登记",不是测试没跑;修法是把跑过的测试写进收据,不是改判定 |
| 11 | 分支分叉 143/75 commits | ❌ 数字错 | `git rev-list --left-right --count ec2/main...HEAD` = **0/335**;main == ec2/main,生产分支是策略分支的**严格祖先**,不存在双向分叉,合并是 fast-forward。143/75 无法在当前仓库复现(可能是过时数据或量错了分支)。"生产机有 untracked 部署文件"无法本地核验,存疑保留 |
| 12 | 生产数据实测(36GB/119GB、7 SEALED、633.8 万笔网球等) | ⚪ 本机不可复核 | 与既有记录(07-10 seal landed、deep02 用 07-12/13)无矛盾,但本审计未重跑生产查询,按"转录未复核"对待 |
| 13 | capture_quality_status=UNASSESSED_PENDING_PIPE_W03,SEALED≠可裁决 | ✅ 与记录一致 | W05 验收 blocked on missing PIPE-W03(SESSION_LOG/记忆);读取链必须显式携带质量等级的要求正确 |

## 命名冲突警告(跨代理传话必踩的坑)

"V3"在仓库里有两个不同的东西:
- **deep03** = 深度研究第三轮(报告里的"Deep Research V3");
- **v3 reference publisher** = W-PUB-REF-01 零拷贝发布器的版本号。
跨代理传话一律写 `deep03` / `pub-ref v3`,禁用裸"V3"。

## "本周上线"的现实(操作员目标 vs 宪法)

**真钱上线本周 = ❌ 自动 reject**,不是进度问题,是宪法问题:
- S1:kill switch 未建未演练(S3),生命周期门未全绿;
- S6:post-only/cancel-on-disconnect/reserve-before-send executor 全未接线;
- Q5:WS 全深度驱动执行(World A/B merge)是 Phase 2 开局,未做;
- D-2:任何 phase 均未授权,W01 还卡在 §31.2 test-isolation artifact;
- F4:研究硬门一条都没过。

**本周可达上限(合宪且有意义)**:
1. **deep03 启动**(探索档本来就不停机)——卡点是研究面只看得到少量
   release,两条解法:(a) 等 W-PUB-REF-01(现状 PROPOSED,还需独立审计+
   操作员 ratify+实现+canary,本周能否完成不确定);(b) **桥接方案:先用
   现行 v2 发布器把 07-14~16 缺的日期补发布**(多花少量 S3 复制成本,
   今天就能解锁 deep03 数据面,零拷贝改造并行不受影响)——操作员成本
   决定,金额级别 ≈ 每日期数 GB × $0.023/GB-mo。
2. **"最基础策略"以影子形态上线**:借 tradingd 现成的 orders_enabled=false
   闸门,把一个最小策略内核(如 PREMATCH-MM 骨架)接到 shadow sink 空转——
   工程冒烟性质,不是正式 shadow 验证,但它同时推进基建(内核+sink 接线
   正是 W-FS1/执行闭环的前半段),与"并行开发"目标完全一致。
3. 干净费用 commit + make check pipefail 修复 + holdout 冻结机制落盘。

微实盘决策 2-3 周的估计:采纳,现实。10-14 天是全绿极限,不是承诺。

## 待操作员裁决清单(不裁则维持现状)

1. **D-5(建议批)**:future-event holdout 冻结机制,自预注册时点起自动积累;
   同 root-event 不跨 split;冻结前禁读 holdout PnL。
2. **D-5b(独立问题)**:该机制是否**替代**"≥20 独立日"晋级门(07-15 冻结),
   还是仅作补充、20 日门保留?
3. **D-6**:F4 的"7 日、5/7"是否改为 root-event 样本量+联赛覆盖+event-block
   bootstrap(D-1 曾驳回类似暂停,需明示推翻或维持)。
4. **W-PUB-REF-01 ratify**(先过独立审计)+ 是否批准 v2 补发布桥接方案
   (含小额 S3 成本)。
5. dirty tree 的干净提交顺序(D-4+费用+哨兵一个 commit;审计文档另一个)。

## 附录 — 被审计报告原文(逐字)

> ## 审计结论
>
> 你说得对:不需要等 20 天才能启动 Deep Research V3。 Research 读取链通过验收后,应立即开跑 V3。
>
> 但目前还不能把任何候选称为"可实盘策略"。距离微实盘决策还差三轮:
>
> 1. V3 把候选经济性算清楚;
> 2. 保守成交模拟器与同一策略内核落地;
> 3. 真实执行、订单状态、风控和故障恢复形成闭环。
>
> ### 生产数据实测
>
> 我在 2026-07-17 直接只读检查了生产机:
>
> - 采集服务当前 active;
> - warehouse 约 36GB,raw 约 119GB;
> - 7 月 10–16 日已有 7 个 SEALED 日期;
> - 7 月 12–16 日:
>     - 网球:633.8 万笔成交、2,730 个 event_ticker、5,774 个市场;
>     - 棒球:189.5 万笔成交、2,964 个 event、7,941 个市场;
>     - 高尔夫:44.7 万笔成交、178 个 event、2,978 个市场;
>     - 网球 L2:约 8,409 万行,覆盖 745 个 event、1,283 个市场;
>     - 上述成交行均有 receive-clock。
>
> 所以真正的问题不是缺数据,而是旧 research 发布路径只让研究端稳定看到少量 release。读取链完成后,这个阻塞可以直接解除。
>
> 需要注意:这些 seal 仍标记 capture_quality_status=UNASSESSED_PENDING_PIPE_W03。V3 可以立即开始,但读取链必须显式携带 gap、L2、时钟和质量等级,不能把 "SEALED" 自动等同于"所有窗口都可做利润裁决"。
>
> ## 当前最紧急的三个修正
>
> 1. 修正 maker 费用假设。
>
> 旧 V3 备忘把体育 maker 当成 0 费,docs/research_reports/DEEP03_SPEC_NOTES.md:18 中的"结构性护城河"结论需要重算。今天实时 API 显示 ATP 网球(KXATPMATCH)、WTA 网球(KXWTAMATCH)、MLB(KXMLBGAME)和 PGA(KXPGATOUR)均为 quadratic_with_maker_fees。
>
> 费率必须按 series/date/fill 查询;未知即拒绝计算。当前费用修订仍在 dirty tree 中,V3 前应形成干净、可复现的 commit。
>
> 2. 立即冻结未来 event-level holdout,不用等日历。
>
> 07-12/13 已暴露,07-15 也已被提前查看,不能再当 unseen。最快办法是:
>
> - 现有可见数据全部作为 TRAIN;
> - 从 V3 预注册时间点开始,将"第一次出现于 cutoff 之后"的完整比赛/root event 自动放入 future holdout;
> - 同一比赛所有市场只能进入一个 split;
> - V3 调参期间禁止读取 holdout 的 PnL 摘要;
> - 候选规则冻结后只打开一次。
>
> 这样 holdout 从现在开始自动积累,不需要空等 20 天。
>
> 3. 旧计划仍有"7 日、5/7 正收益"条款。
>
> 它不阻塞 V3 启动,但会在 V3 后继续阻塞 shadow,docs/PLAN_MM_TEST_PROGRAM.md:554 仍然有效。按你本轮裁决,建议将它同步改成:
>
> root-event 有效样本量 + 联赛/regime 覆盖 + event-block bootstrap
>
> 日期保留为压力分层,不再作为硬计数。否则后续 agent 仍可能重新开始等日期。
>
> ## 最短关键路径
>
> 设 T0 = Research 读取链 canary 通过:
>
> | 目标 | 最快路径 | 估计 |
> |---|---|---|
> | 启动 V3 | 读取验收+预注册+费用修正 | T0 当天 |
> | 得到可冻结候选 | V3 三族对比+保守成交初算+操作员审核 | T0+2–4 天 |
> | 可进入正式 shadow | W-FS1+同一 C++ 策略内核+执行安全闭环 | T0+5–9 天 |
> | 可由操作员决定微实盘 | 5 个完整 shadow 绿日+故障演练+1 张探针 | 最快 T0+10–14 天 |
> | 可考虑放量 | 再完成两周微实盘校准 | 至少再加 2 周 |
>
> 现实预期:微实盘决策约 2–3 周。10–14 天是所有并行工作一次通过的极限情况,不是收益承诺。
>
> ### V3 应同时检验三族,但不要提前指定胜者
>
> - TENNIS-90-RANGE:88–93 区间双边报价+L2 撤退信号。数据最丰富,优先研究;但撤单竞赛、比分失明和延迟风险最高。
> - PREMATCH-MM:赛前低竞争市场的 post-only maker。若通过,最适合成为首个微实盘候选,因为执行复杂度较低。
> - CERTAINTY-SUPPLY:85–99¢ 确定性供给。必须有真实结算、爆冷尾损和 CVaR;在这些数据齐全前只能 diagnostic。
>
> V3 输出应止于"候选档案+淘汰理由",最终冻结哪个策略仍由操作员把关。
>
> ## 工程侧不能跳过的闭环
>
> 当前 C++ roster 只有一次性 once_probe,默认空策略,src/strategies.cpp:13。真实 tradingd 仍然直接签名发送,没有接 RiskLedger、OrderManager、reconcile;普通策略单默认也不是 post-only,apps/tradingd.cpp:265。
>
> 微实盘前必须完成:
>
> - receive-clock tape → W-FS1 悲观成交模拟;
> - 同一个策略内核连接 replay、shadow、live 三个 sink;
> - 唯一 OrderSubmitter,逐单经过 operator lease、风险 reserve、限频和 freshness;
> - 持久化订单状态机,覆盖 ACK、partial fill、cancel、unknown、reconcile;
> - WS L2、private fills/user orders、pause/断线先停新风险;
> - 强制战略报价 post_only=true、cancel_order_on_pause=true,并使用交易所 order group(官方 API:Create Order V2、Order Groups、维护与暂停文档);
> - panic、重启、429、断线、ACK 丢失、部分成交全部故障注入通过。
>
> ## 测试审计
>
> 我实际运行了:
>
> - make check:通过;
> - tests/run_pipeline.sh:通过;
> - 未纳入主流水线的 4 个测试文件:34/34 通过。
>
> 但当前绿灯还不能作为上线证据:
>
> - make check 使用 test | tail -1 且没有 pipefail,前面的测试失败可能被掩盖,Makefile:375;
> - readiness 仍显示 blocked,并把部分刚通过的测试判成 not_started,tools/lifecycle_check.py:299;
> - 测试收据没有绑定完整 commit、dirty diff、配置、运行完成标记;
> - 策略分支与生产分支已分叉 143/75 commits,生产还有无法由 active HEAD 重建的 untracked 部署文件。
>
> 因此建议下一轮只并行推进四件事:
>
> 1. Research 读取链 canary;
> 2. V3 预注册、future-event holdout 和 maker-fee 修正;
> 3. W-FS1+同一策略内核;
> 4. OrderManager/Risk/WS/reconcile/panic 执行闭环。
>
> 最终判定:V3 可以立即准备、不等 20 天;当前 live 仍应保持关闭。最快先在 2–4 天内敲定"值得工程化的候选",再争取 2–3 周进入由操作员决定的一张 post-only 微实盘。
>
> 本次仅做审计和只读检查,没有修改业务代码、没有发送订单。
