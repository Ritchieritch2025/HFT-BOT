# 提案:方向调整——体育优先 + 基础设施重心 + 快速试错回路

- 日期:2026-07-09。性质:**提案,非生效计划**。生效须由 fresh session
  走计划变更 W + 独立审计(GUARDRAILS 流程),修订 MM_ROADMAP 1.5A/1.5C。
- 依据:操作员三条方向(均已 E2 落盘于 RESEARCH_EDGE_HYPOTHESES §4.5):
  ① 体育是目标市场;② 不投入复杂预测模型,重心 = 实时 feed + 映射 +
  工程细节;③ 原 crypto 计划缺乏市场参与者反馈,现依据五集访谈情报
  (RESEARCH_INTERVIEW_* 五份)调整并快速试错。

## 一行裁决

✅ 调整成本低:管道/仓库/AWS 迁移/风控门全部品类无关,原样保留;
真正改的只有两处——**锚的来源**(现货指数 → sportsbook 赔率 feed)
和 **1.5 工作量分配**(砍预测建模,加映射层与报价漏斗);
"快速试错"的合规形态 = 已有数据先裁决 + shadow 快迭代,
悲观口径和上线闸门一个不减。

---

## 1. 不变的(已建资产全部保值)

- 24/7 管道本就采集**全市场**(含体育)L1+trades——体育数据已在
  仓库积累,零返工。
- MASTER_SEQUENCE 现有步骤不动:STEP 1 AWS 迁移是延迟基础,
  体育比 crypto 更需要;timestamp-ladder(BACKLOG,VERY IMPORTANT)
  是任何品类回测的前提。
- 品类无关的硬规矩全保留:log-odds 空间、费用进模型、悲观成交
  口径 = go/no-go、E4 定点数、风控闸/kill switch/影子验证的
  阶段结构。

## 2. 改的(两处)

**A. 锚:1.5A 的"现货指数"换成"sharp sportsbook 赔率 feed"。**
- 待操作员决策(花钱项,届时呈选项+报价):买商业 feed
  (unabated/OpticOdds 类,月费,稳定但同质)vs 自建爬虫
  (Eggsy 路线:更快、独家、脆、有 ToS 风险)。
- 新增准入检查(H13 体育版):锚源限额 vs Kalshi 可套体量比值。

**B. 1.5 工作量重排:**
- 砍:预测建模、复杂 ML(依据:五个盈利样本零自建模型,
  W-L3595 欠拟合原则)。
- 加:①映射层(feed 名称 ↔ Kalshi ticker,带红字先行测试 +
  变更告警;T-L1407 最致命组件)②报价漏斗全量日志(含拒报
  决策;T-L1697 静默丢单教训)③防守标定(毒性分桶/规模-毒性/
  因子敞口,轻量统计,不可砍)。

## 3. 快速试错回路(合规形态)

**试错 ≠ 提前实盘。** Eggsy 的 "let it rip" 是单人赌自有资金;
我们的等价物是把迭代压进数据和影子层,闸门不减:

- **W-S1(第一个 W,零采购,数据现成):品类对比扫描。**
  扩展 mm_scan:用仓库既有数据,把 sports(NBA/NFL/MLB/NHL/soccer
  等 Kalshi 类目)vs crypto 并排量化——价差 × 深度 × 成交到达率 ×
  毒性(成交后漂移)× 散户特征(小额/整手/价格带聚集)。
  产出:数据裁决先做哪些具体市场,替代 H7。
  退出条件:一份带数字的品类/市场排序表,悲观口径估算毛利。
- **W-S2:feed 选型呈报**(选项+价格+延迟实测),操作员拍板。
- **W-S3:映射层 + feed 接入 + 漏斗日志**(红字先行,新行为新测试)。
- **W-S4:体育 shadow**(原阶段 2 的门原样:影子连续 5 交易日正
  PnL,最大回撤 < 单日均利 3 倍)。
- 微实盘及以后:沿用原阶段 3/4 闸门 + "诚实规模"分级放量协议
  (H4 附注)。

每个 W 一个 fresh session、事后独立审计——这本身就是快速试错:
快在反馈回路短,不快在跳过检验。

## 4. 需要操作员现在拍板的

1. 本提案是否作为计划变更 W 的输入?(是 → 下个 session 执行
   MM_ROADMAP 修订 + W-S1)
2. feed 预算量级的心理准备(不必现在定数,W-S2 时给选项)。

## 5. 风险登记(不因方向调整而消失)

- 监管尾部:体育是 Kalshi 被诉核心(E-L2249),方向集中即尾部
  集中;crypto 管道照采,保留回退选项,成本≈0。
- 锚成本与同质化:人人接得起商业 feed → edge 全在执行与防守,
  正是重心所在,但要清醒:买 feed 不构成 edge,只构成入场券。
- courtside/latency:体育品类 H3 熔断为开仓闸门,W-S1 的毒性
  测量直接为其标定。
- **Q7 冲突登记(审计 F5)**:GUARDRAILS Q7 将 MVE/combo 排除于
  MM 候选,而访谈最强收入端(RFQ combos)正是 MVE 类。W-S1 阶段
  Q7 照常生效;远期走 RFQ 须正式修订 Q7(操作员批准的宪法修改,
  单独 commit + rationale)。在此登记,防止未来 session 无感违宪。

---

# v1.1 修订(2026-07-10,依据 docs/plan_audits/audit_PROPOSAL_DIRECTION_ADJUST_2026-07-10.md,9 findings 全部修复)

## 附录 A:W-S1 七字段任务卡(品类对比扫描)

**Purpose:** 用既有仓库数据量化 sports vs crypto 的做市适宜度,
产出带数字的品类/市场排序表,替代 H7,为方向裁决与 feed 预算
提供数据依据。

**Allowed reads:** EC2 仓库(staging + archive)或 S3 副本;
dim/catalog;docs/RESEARCH_EDGE_HYPOTHESES_2026-07-09.md。
**数据面(审计 F1):在 EC2 执行**(cutover 后 EC2 为唯一数据主人;
Mac 仅存 07-06..08 三天)。16GB 盒子 ⇒ 按 category×date 分块处理,
禁止全量载入。D6:读者重试,不与 pipeline 写者争锁。

**Allowed writes:** `tools/mm_scan.py`(扩展品类对比模式)、
`tests/test_mm_scan_categories.py`、输出报告
`work/research/category_scan_<date>.md` + 伴随 CSV。tools.json 若加
子命令需登记(E3)。

**Forbidden writes:** pipeline/capture/ingest/export、策略数学
(Q1/Q3 不动)、MM_ROADMAP/MASTER_SEQUENCE、config/*。

**指标定义(全部 log-odds 空间,交易所时钟,标注"非本地钟"限制):**
按 category×subcategory×市场:①价差:touch spread 分布(中位/p25);
②深度:touch 双边挂量;③到达:trades/小时、笔均手数;④毒性:
成交后 mid 漂移 Δlo(30s / 120s),按 taker_side 符号化;⑤散户特征:
小额占比(≤10 手)、整手聚集、价格带聚集(W-L799 假设)、时段模式;
⑥Q6/Q7 过滤后的候选数。输出 = 悲观口径毛利代理:
中位半价差 − |毒性均值|(120s),按品类排序。

**Acceptance(运行演示,非描述):** 在 EC2 对 ≥3 个完整日运行,
打印品类对比表(Sports 各 subcategory vs Crypto 至少两行以上),
每个数字带样本量;`make check` 绿;新测试
(fixture 数据 → 确定性指标值)绿。**三天数据结论标注 INTERIM;
七天 gate(2026-07-13)后重跑同一命令出正式版。**

**Risks:** 世界杯扭曲体育样本(审计 F7)——结论按"WC 时段"显式
标注,同时单列 WC 子样本作为散户流上界估计;交易所时钟测毒性
只支持品类**相对**比较,绝对延迟结论等 timestamp-ladder。

**Rollback:** revert commit;删除 work/research/ 产物。若结果显示
体育在悲观口径不优于 crypto ⇒ 决策点回操作员,方向回退成本≈0
(crypto 管道未停,审计 F8)。

## 附录 B:执行顺序与交接引文(审计 F3/F4 修正)

顺序:操作员批准本提案 → **session A(纯纸面,P9)**:修订
MM_ROADMAP 1.5A/1.5C + 起草 MASTER_SEQUENCE amendment(W-S2+ 插入
位置)+ 同步受影响文档(MM_ROADMAP、OPERATOR_PLAYBOOK;审计 F9)
+ 合并独立审计 → **session B**:执行 W-S1(按附录 A,一个 W 一个
session)→ W-S1 数字出来 → 操作员裁决品类权重 + feed 预算(W-S2)。

W-S1 与 MASTER_SEQUENCE 关系:纯读研究,不占 STEP 序列、不阻塞
STEP 2-6;W-S2 起须以 operator-approved amendment 进 MASTER_SEQUENCE。

**交接引文(session A,纸面):**
> 读 docs/GUARDRAILS.md、docs/PROPOSAL_DIRECTION_ADJUST_2026-07-09.md
> (含 v1.1 附录)与其审计后,执行纸面计划变更 W:修订 MM_ROADMAP
> (体育优先、1.5A 锚换 sportsbook feed、1.5 工时重排,保留 crypto
> 回退注记),起草 MASTER_SEQUENCE amendment(W-S2+ 插入),同步
> OPERATOR_PLAYBOOK。纯文档,不执行代码。P9 合并独立审计 +
> 退出仪式。

**交接引文(session B,W-S1):**
> 读 docs/GUARDRAILS.md 与 docs/PROPOSAL_DIRECTION_ADJUST_2026-07-09.md
> 附录 A 后,在 EC2 上执行 W-S1 品类对比扫描,严格按七字段任务卡
> (Allowed/Forbidden writes、Acceptance 原文为准)。数据不足七天
> 则出 INTERIM 版并注明。退出仪式 + 独立审计。

## 附录 C:W-S2 评估标准(审计 F6,呈报模板)

候选 feed 每项五栏:①延迟:同一事件 feed 时戳 vs Kalshi 盘口反应
(本地钟,采样 ≥100 事件);②覆盖:Kalshi sports 活跃市场匹配率;
③价格质量:与 Kalshi 盘口中价偏离分布 + 更新频率;④法务:ToS
允许用途、转售限制、封禁风险;⑤成本:月费 + 超量计价,预算上限
由操作员定。爬虫方案同表评估,加"脆性"栏(选择器变更频率、
反爬对抗成本)。
