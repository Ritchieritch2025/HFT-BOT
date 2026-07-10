# PLAN: 第一轮研究循环(操作员手把手版,2026-07-10 操作员批准)

目的:把近期确定的全部测试收进一个可顺序执行的计划。每一步:
操作员粘贴引文/敲命令 → 产出落盘 → **回 Cowork 会话读数**(由 Cowork
逐个数字讲解)→ 满足条件才进下一步。
性质:研究(T2 只读为主)+ 一项测量任务;不碰生产、不动
MASTER_SEQUENCE 既有步骤;悲观口径与全部闸门不变。
关联档案:sandbox/research/pilot_maker_edge/PILOT_SPEC.md(操作员
十点规格)、RESEARCH_EDGE_HYPOTHESES(H1-H13)、PLAN_PRICING_MODEL
Group C(正式校准,gated ≥2026-07-13)。

---

## S0 · 前置(日历事件,已排)
- STEP 1 收官验收(24h 零缺口)→ 通过后同一维护窗口:apt 升级 +
  重启 + **W-TL1 部署上 EC2**。自此 recv 时间戳开始在生产积累。
- 状态:待操作员今晚执行(引文已在手)。
- 维护窗口纪律:一次停机、一个缺口;缺口在 capture_gaps 与
  quality_log 标注 deliberate-maintenance。
- **"净数据日"定义钉死**:当日除已标注维护缺口(合计 ≤15 分钟)
  外无未解释缺口 = clean。否则今晚的刻意重启会静默重置 07-13
  七天门;若后续校准会话另有更严定义,以先落盘者为准并知会操作员。
- ✅ **S0 验证(全过才算完)**:
  ① 24h 窗口:EC2 上 `capture_gaps --date 2026-07-10` 无未解释缺口;
  ② 维护后:`systemctl status kalshi-pipeline` = active;今日 raw
    分片两次 `ls -la` 字节数在涨;journalctl 无 SIGKILL(优雅退出实测);
  ③ TL1 生效铁证:staging 三表 DESCRIBE 出现四列;**维护后新行
    recv_wall_ns 非 NULL**(抽查 SQL),heartbeat 行四列 NULL 且
    ts_utc=整点;④ 维护缺口已在 capture_gaps 标注 deliberate。
  🚩 红旗:部署后新行 recv 仍全 NULL = ingest 没换代码或没重启。

## S1 · Maker-edge pilot 完整版(spread − markout − fees 分解)
- 目的:H1 第一次开庭——赛前网球的价差收入是否盖得住毒性。
- 定位(操作员 2026-07-10):**网球 = 滩头,不是疆界**——选它因为
  比赛密度/样本量最大、常规巡回赛平稳;pilot 产出的是可复用模板,
  S4 用它横扫全品类出排名表,后续品类扩张照表点名,数据驱动。
- 执行:操作员在 VSCode 粘给 Claude Code(引文见附录 A)。
- 产出:DQ 报告 + 分解表(spread capture vs mid-口径 markout,
  含 bounce/drift 拆分)+ 盘口级 burstiness + 交互 HTML,落
  work/research/。
- **管道即产品**:S1 交付的不是一次性脚本,而是参数化可复跑的
  研究管道(注册进 tools.json):一条命令 = 切片物化 → join
  细分维度表 → 指标计算 → 交互 HTML。验收:换参数(品类/日期)
  重跑即得 S4/S5,**零代码分叉**。
- 读数:回 Cowork,逐表讲解。**S1(dev-grade)合法结论仅两种:
  methodology-valid + collect / methodology-flawed(修后重跑);
  trade/reject 判决权只属于 S5**(capture_host 规则,自洽修正)。
- 注意:Mac-era 数据 → 结论标 dev-grade;方法学在 S5 用 EC2 数据重跑。
- 选品钉死(操作员 2026-07-10 裁决,取代原"仅零 maker 费"条目):
  S1 范围 = 全部网球 match 系列(KXATPMATCH / KXWTAMATCH / KXITFMATCH /
  KXATPCHALLENGERMATCH,及数据实测存在的 KXITFWMATCH / KXWTACHALLENGERMATCH),
  按 dim_segments 的 maker_fee_class × tour_level 分层。
  · 主指标(纪律 1)每层各算各报,**禁止跨层合并出单一总数**——零费层
    与收费层是两个物种。
  · 收费层的 edge 分解**额外扣一列 maker 费**,按纪律 12 费用预览管道
    内联计算(taker/maker 公式 + ceil_to_centicent),输出强制带
    NON-GATE / verified=false 横幅;OQ-1 未批,一切 facts-gated 工具的
    门禁不动。
  · H1(费率墙)检验**仅在零费层内成立**,报告写明。
  · 查证来源不变:kalshi_facts.yaml 费率节 + catalog/series 的 fee_type
    (live GET /series);series 未列明 = maker_fee_class=unknown,
    fail-closed 剔除(Q3)。
  · 本修改发生于预注册冻结之前;预注册按新口径一次写成。其余 12 条
    纪律、每步验证门、七图、S1 判决限制(methodology-valid+collect /
    methodology-flawed)全部不变。
- ✅ **S1 验证(机器门 + 五眼清单)**:
  机器门:make check + run_pipeline 绿;**恒等式自检**——每桶
  bounce+drift 与 markout_total 之差 < 0.01¢(纪律 11 的代数保证,
  不满足 = 算法写错);剔除计数器(陈旧/无市场/locked)全部入报;
  悲观成交 n 打印且与 200 阈值比对;产出带指纹头。
  操作员五眼清单(缺一退回):① 报告开头有"预注册"节且在结果
  之前;② 每个数字旁有 n;③ 结论 ∈ {methodology-valid+collect,
  methodology-flawed};④ regime=slam-week 标签在;⑤ 指纹头
  (SHA+manifest md5+命令行)在。

## S2 · 反应秒表·上半(本地可测,无风险)
- 目的:回答 Rhys 问题#3 的"我方钟":签名到底多贵。
- 执行:RSA-PSS 签名基准 **Mac 与 EC2 各跑一份**(生产为 ARM
  盒子,config 采用 EC2 数字;p50/p99,≥1e4 次),
  结果写进 config/backtest_latency.yaml 替换对应 PLACEHOLDER,
  注明 MEASURED + 日期 + host。基准必须打在**生产同款签名路径**
  (openssl RSA-PSS,同 src/client.cpp 用法与密钥规格)——测错库
  = 数字作废。纯本地计算,不碰网络。
- 读数:回 Cowork——签名占整个反应预算的几成,值不值得优化。
- ✅ **S2 验证**:① yaml diff 显示 PLACEHOLDER→MEASURED(+日期+host),
  数值 = 报告数值(抄写恒等);② 复跑一遍 p50 偏差 <30%(稳定性);
  ③ 合理性区间:RSA-PSS 签名 p50 预期落在 0.1–5ms——快得反常
  (<50µs,可能测了错误路径/缓存)或慢得反常(>20ms)都是红旗,
  先查再录;④ 基准脚本入库并注册 tools.json(E3)。

## S3 · 反应秒表·下半(需操作员批准后执行)
- 目的:signed-POST 全链 RTT p99(真实往返,不碰真单)。
- 执行:执行会话先提交采样方案(端点选择、频率、只读性论证)
  → **操作员过目批准** → **从 EC2 发起**,≥3 个时段各采样 ≥100 次
  (RTT 是分布不是快照;全程低频,采样间隔 ≥2s,远低于 rate
  预算)→ 替换 RTT PLACEHOLDER。
- 读数:回 Cowork——合成 order_effective 总预算,对照 S1 的
  市场心跳表,正式回答"我们比市场快几倍/慢几倍"。
- ✅ **S3 验证**:① 采样日志:≥3 时段 × ≥100 次,时间戳可查,
  QPS ≤0.5;② 零副作用铁证:采样前后各跑一次
  `account_view --assert-zero-resting`(W-K1 工具),两次都 exit 0
  ——用自己的 kill-switch 眼睛证明没碰真单;③ 合理性区间:
  us-east-2 内 RTT p50 预期 1–50ms,<0.5ms(打到缓存/错 host)或
  超时率 >1% = 红旗;④ 分时段 p50/p99 各自入报再合并。

## S4 · 盘口级 burstiness + 品类横向对比
- 目的:Rhys 问题#2 的完整版(quote 更新比成交更密)+ 品类对比
  (sports 各子类 vs crypto)。
- 执行:**单独一场**(全品类 L1 体量大,16GB 盒子按品类×日期
  分块;S1 的 burstiness 只含 Tennis,此处才做横向)。
- 读数:每品类一行:心跳中位/p99、爆发系数、与我方预算的倍数。
- ✅ **S4 验证**:① 每品类样本量 + 与 manifest 行数对账(抽 2 个
  品类核对,差异 >1% = 数据没读全);② 无 OOM(分块纪律的证明);
  ③ 指纹头;④ Tennis 行与 S1 内部结果一致(同数据同口径,
  数字对不上 = 两处算法漂移)。

## S5 · EC2 时代重跑(唯一有资格进 go/no-go 的版本)
- 门:≥2026-07-13 七天净数据 + recv 列积累(实际 ≥07-17 成熟)。
- 执行:同 S1 的命令在 EC2-era 数据上原样重跑(capture_host=EC2);
  markout 双钟并报(exchange 与 recv),**主判决用 recv**;
  产出替换 dev-grade 版本,并做 slam-week vs 常规赛跨 regime 对比。
- 切分:选样冻结于 EC2-era 最早段,val = 中段;**最近 2 天留作
  test,方法在 val 定稿后只开封一次**。
- 读数:回 Cowork,与 dev-grade 版对比——结论变没变,为什么;
  **trade/reject 判决只在此步产生**。
- 此后接 Group C 正式校准(PLAN_PRICING_MODEL,recv-only,无例外)。
- ✅ **S5 验证**:① 开跑前门检:按 S0 钉死的 clean 定义逐日核对
  七天 + EC2-era recv 覆盖率 ≥95%(SQL 入报);② test 窗开封
  记录:报告注明开封时刻,且 val 版报告的指纹早于开封时刻
  (只碰一次的可查证据);③ 双钟表并排,主判决标注 clock=recv;
  ④ 与 dev-grade 版逐桶对照表(方向翻转的桶必须逐个解释);
  ⑤ 结论 ∈ {trade, reject, collect}。

## S6 · 订单行为实证(W-K6,操作员排期)
- 目的:Rhys 问题#4 的收尾——订单类型行为与文档一致性
  (post-only 拒单、到期自灭、client_order_id 409 去重)。
- 门:操作员排期 + 注资;按 PLAN_RISK_KILLSWITCH W-K6 原文执行,
  全程 operator-gated(S1/S3)。
- ✅ **S6 验证**:按 W-K6 自带 Acceptance 原文;外加前后各一次
  assert-zero-resting;每个订单行为断言(post-only 拒单/到期自灭/
  409 去重)= 一条实测记录,假设与实测不符 ⇒ 更新 kalshi_facts
  并回改依赖该假设的代码。

---

## 每步通用验证(读数时执行,2026-07-10 追加)

- **三查**:任何产出先查三样——指纹头在不在、每个数字旁边有没有
  样本量、剔除有没有计数。缺任何一样,退回,不读内容。
- **Cowork 复算抽检**:每步读数时,Cowork 用独立路径(csv.gz
  流式/另一种写法)重算 ≥1 个核心数字;偏差 >5% = 打回执行会话
  对账。两条独立管道算出同一个数,才配叫"验证过"。
- **红旗直觉**:任何"好得离谱"的数字默认是 bug 不是 alpha,
  先找错再庆祝(访谈铁律:大亏都来自工程错误)。

## 统计与可信性纪律(2026-07-10 审计后追加,约束 S1/S4/S5 全部分析)

1. **预注册主指标(唯一判决数)**:赛前时段、成交量加权的
   `半价差 − 30s 中价 markout`(¢/张,悲观口径)。开跑前钉死;
   其余全部桶为探索性,只产假设不产结论。
2. **Regime 标签**:07-06..08 = Wimbledon slam-week;所有结论携带
   `regime=slam-week`,禁止外推常规巡回赛;S5 做跨 regime 对比。
3. **选样/测量分离(通则)**:名单只在最早时段(train)冻结,
   其后时段只测不选。S1 实例:train=07-06/07,val=07-08;
   S5 实例见其切分条目(含一次性 test 窗)。
4. **不确定性 = 按比赛整场的块状 bootstrap**(≥1000 次重抽),
   禁止按笔独立假设算标准误(成交高度聚簇,CV≈6-8)。
5. **中价卫生**:L1 LOCF 补价带最大陈旧度上限(默认 60s,超限
   剔除并计数);**无市场状态判据 = 任一侧挂量为 0,或 spread
   ≥20¢**(禁止按"ask≥99¢"一刀切——真实长尾市场合法住在
   97-99¢);locked/crossed 剔除;一切剔除计数并报。
6. **tick 一致性**:1¢ 与次美分 series 分开统计,pilot 主表限 1¢。
7. **因果分界**:赛前/开打探测器只用截至当时的信息,参数在
   train 段冻结后原样用于 val。
8. **可复现指纹**:每份产出头部 = code commit SHA + 数据文件
   manifest md5 清单 + 完整命令行。
9. **性能纪律**:网球切片一次性物化为 parquet(work/research/
   下,derived 可重建);一切查询带 category/subcategory/date
   分区过滤。

10. **最小样本预注册**:悲观口径模拟成交数 n<200 ⇒ 结论自动 =
    collect more data,禁止解读主指标(小样本的漂亮数字是噪声)。
11. **bounce/drift 公式钉死**(sign=taker 方向,maker 视角取负):
    bounce_i = sign×(p_fill,i − mid(t_i));
    drift_i(h) = sign×(mid(t_i+h) − mid(t_i));
    markout_total ≡ bounce + drift。分解只此一种算法。
12. **费用预览管道**:研究侧费用曲线按文档化公式内联计算,强制
    NON-GATE / verified=false 横幅;**禁止改动任何 facts-gated
    工具的门**(OQ-1 未批前正式费用计算继续拒绝)。

## 市场细分维度表(2026-07-10 操作员要求:细分成体系,一次建好)

所有研究分析**必须 join 同一张细分维度表**,禁止各脚本临时自切:

- 落点:`work/research/dim_segments.parquet`(derived,可重建),
  由 catalog/series_classified + ticker 解析生成,建表脚本入库注册。
- 维度列(细分层级,缺一即 DQ FAIL):
  ① category / subcategory(仓库已有);
  ② **tour_level**——从 series 前缀解析,网球实测存在:
     KXATPMATCH / KXWTAMATCH / KXITFMATCH / KXATPCHALLENGERMATCH
     → ATP / WTA / ITF / Challenger(其他运动同法:联赛级别);
  ③ market_kind——胜负盘 / 总分类 / 让分类 / 其他(由 series
     规则文本与 ticker 形态归类);
  ④ tick_stratum——1¢ vs 次美分(按该 series 实测价格粒度);
  ⑤ maker_fee_class——零费 / 收费 / 未知(kalshi_facts 查证,
     未知按 fail-closed 剔除);
  ⑥ event_id 与 phase 边界(赛前/in-play 因果探测器输出)。
- DQ 约束:每个 market_ticker 必须映射到**恰好一个**细分组合;
  未能归类的进 `_unsegmented` 桶并计数(>2% 即 FAIL 修表)。
- 一切图表、一切统计的分组键 = 此表的列,别无来源。
  (毒性、价差、心跳全部按 tour_level 分开看——ITF 深夜盘和
  ATP 黄金档是两个物种,混在一起 = 平均出一个不存在的市场。)

## 可视化规格(2026-07-10 操作员要求:每步必出图)

**渲染标准(2026-07-10 操作员升级:必须动态交互)**:
自包含 HTML + **本地 vendored 交互库**(Plotly.js 或 ECharts,
库文件一次性入库到 docs/vendor/js/,HTML 引用本地文件——
仍然无外部 CDN、离线可开、可版本化)。交互能力下限:悬停显示
精确值与 n、框选缩放、图例点选显隐序列、**按细分维度切换/过滤**
(至少:tour 级别、价格带、tick 层、赛前/in-play)。每图标题 =
它回答的那个问题;n 与 CI 上图;页脚指纹头。归档需要时可另导
静态 PNG/SVG 快照,但交互版是正本。dev-grade 与 EC2 正式版同
配色同坐标,肉眼可比。

**诚图五规**(违反任一 = 图作废重画):
① 一图一问,标题就是问题;② 不确定性上图(bootstrap CI 带,
不许只画均值线);③ markout/edge 类图必画零线,y 轴不许无标记
截断;④ 禁双 y 轴;⑤ horizon 用对数轴,金额单位一律 ¢。

**S1 必出七图**:
1. **毒性曲线**(镇报之图):x=horizon(1s/10s/30s/120s,log),
   y=markout ¢,按价格带分线,CI 带 —— "被打之后价格往哪走、走多远?"
2. **bounce/drift 分解堆叠图**:每个 horizon 一柱 —— "taker 付的是
   价差弹跳还是真信息?"
3. **赛前价差分布直方图**(按 tick 层分面)—— "摆摊的毛利空间长什么样?"
4. **主指标瀑布图**:半价差 → −markout → −费用 → 净 edge/张,
   带 CI —— "这门生意每张赚几分?"(判决图)
5. **时段热力图**:小时 × 星期,成交量与价差 —— "散户几点来?"
6. **逐场散点**:x=该场价差收入,y=该场 markout,一点一场 ——
   "edge 是普遍的还是被几场极端值扛着的?"
7. **DQ 图两小张**:剔除计数柱状 + mid 陈旧度直方 —— "数据干净吗?"

**S2 图**:签名耗时直方(log x),p50/p99 竖线,Mac vs EC2 叠画。
**S3 图**:RTT 分时段 CDF 叠画 + 采样时序散点(看漂移)。
**S4 图**:品类小倍数(每品类一格:盘口更新间隔 CDF,log x)+
  "心跳 vs 我方预算"散点:x=品类心跳中位,竖线=order_effective
  预算 —— 线左边的品类追不上,线右边的能做。
**S5 图**:S1 七图重出(EC2 数据)+ **哑铃图**(每桶一根:dev-grade
  端点 → EC2 端点,看方向翻转)+ slam vs 常规赛 markout 曲线并排。
**S0 图(可选)**:部署后 recv 覆盖率随时间爬坡线(确认 TL1 活着)。

## 附录 A · S1 引文(粘给 Claude Code)

> 读 sandbox/research/pilot_maker_edge/PILOT_SPEC.md(操作员十点
> 规格)与 docs/PLAN_RESEARCH_CYCLE_1.md S1 后,在本机用 duckdb +
> tools/warehouse.load() 执行完整 maker-edge pilot:
> 品类 = Sports/Tennis 赛前时段,2026-07-06..08 按时间顺序切
> train/val;mid 口径 markout(1s/10s/30s/120s),把 markout 分解
> 为 bid-ask bounce 与 true drift 两项(公式 = 纪律第 11 条,
> 只此一种);fee 按纪律第 12 条内联预览 + NON-GATE 横幅
> (OQ-1 未批,不许动 facts 门);
> 选品限零 maker 费 series(逐 series 查证);成交模型假设显式
> 成段写出(join-the-touch 悲观/乐观双口径,无队列数据 = 悲观
> 按队尾;悲观成交数 n<200 ⇒ 自动 collect,纪律第 10 条);
> 附盘口级 inter-update 间隔分布(赛前 vs in-play,**限 Tennis**,
> 全品类横向属 S4 另场)。产出 DQ + 结果表 + HTML 落 work/research/,每个数字
> 带样本量与分位数;**结论仅允许 methodology-valid+collect 或
> methodology-flawed(trade/reject 保留给 S5)**。产出仅落
> work/research/;运行过长可按文件断点分两场续跑。Mac-era 数据结论
> 标注 dev-grade(regime=slam-week)。**严格执行本计划
> 「统计与可信性纪律」全部 12 条与「每步通用验证」**(预注册
> 主指标、最小样本、bounce/drift 恒等式自检、选样/测量分离、
> 按场 bootstrap、中价卫生、tick 分层、因果分界、指纹、parquet
> 物化+分区过滤、费用预览不动门);主指标未预先声明即开算、或
> 恒等式自检缺失 = 审计 REJECT。
> 先建「市场细分维度表」(dim_segments.parquet,tour_level/
> market_kind/tick_stratum/maker_fee_class/phase,每 market 恰好
> 一组,_unsegmented >2% = FAIL),一切统计与图表按此表分组;
> HTML 必含 S1 七图且为**动态交互版**(vendored Plotly/ECharts
> 本地库,无 CDN;悬停出值+n、缩放、图例显隐、按 tour/价格带/
> tick 层过滤;诚图五规不变);整套分析交付为参数化管道并注册
> tools.json——换参数即 S4/S5,零代码分叉。
> 退出仪式 + 独立审计。

## 执行台账(操作员勾选)
- [ ] S0 收官 + TL1 部署
- [ ] S1 pilot(dev-grade)→ 已回 Cowork 读数
- [ ] S2 签名基准 → 已回读
- [ ] S3 方案批准 → RTT 实测 → 已回读
- [ ] S4 盘口 burstiness → 已回读
- [ ] S5 EC2 重跑(正式版)→ 已回读
- [ ] S6 W-K6(另行排期)
