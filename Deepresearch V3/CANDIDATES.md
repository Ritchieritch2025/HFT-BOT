# deep03 候选清单 — 人类可决策版（CANDIDATES）

**裁决（一行）：⚠️ 有 2 个信号活下来、1 个勉强、3 个被否。唯一够格排第一进 ≥20 天验证门的是「深度回补做市」（refill-timed requoting，机制2）。全篇为 EXPLORATORY_ONLY，禁止任何盈利结论。**

- 数据口径：8 个封存日 2026-07-10..17。L2（盘口深度）只有 3 天干净：**07-12 / 07-15 / 07-17**。07-13/14/16 因数据质量被排除；07-10/11 根本没抓 L2。所有 L2 估计只建立在这 3 天上。
- 来源：`report12/RESULTS.json`（4.3MB 全量）、`report12/FULLSCOPE_L2_REPORT_TABLES.json`（L2 汇总表）。每个数字后面都标了它来自哪个字段。
- 声明层级（`RESULTS.json.conclusion_status` = `DESCRIPTIVE_FULLSCOPE_BASE_L2_COMPLETE`，`l2_report_tables.claim_tier` = `DESCRIPTIVE_CLEAN_DATES_ONLY_NO_PNL`）：只做描述，不做费后盈亏、不做置信区间、不做政策判定。
- 提醒：deep01 已经把「S1 纯做市」判成**费后负期望**。所以下面出现"毛口径为正、费后可能被吃掉"是诚实的、不是失败。

---

## 术语表（GLOSSARY — 每个词一句白话，English 标识符保留）

- **markout（成交后价格漂移）**：一笔成交发生后，中间价在未来 X 秒里往"主动方（taker）"方向又走了多远。为正 = 价格继续朝吃单方向跑 = 做市方（挂单方）被"抬走/砸穿"= 逆向选择成本。
- **log-odds（对数几率空间）**：把概率价格 p 换算成 ln(p/(1-p)) 后再算价差/漂移。项目规矩：策略数学一律在 log-odds 里做。近 50¢ 时，1 个 log-odds 单位 ≈ 25¢；所以 0.04 log-odds ≈ 1¢（乘 0.25 的粗略换算，仅供直觉，不是精确 tick）。
- **half-spread（半价差）**：买卖价差的一半，就是你挂在盘口一侧、如果对手来成交、你名义上赚到的那部分（毛口径，未扣费）。
- **refill hazard（回补风险率/回补速度）**：盘口某一侧顶部深度被吃掉≥50% 之后，在接下来某个 100ms 小窗里"深度重新补回到≥80%"这件事发生的概率。越高=流动性回补越快。
- **cumulative refill P（累计回补概率）**：从 refill hazard 用生存曲线（product-limit）累出来的"在 t 毫秒内补回来的总概率"。
- **retreat episode（撤退/抽深事件）**：定义为同一侧最优 3 档显示深度里被撤掉≥50%、且撤掉量≥1¢名义（`stage_abi.top3_retreat_definition`）。用来找"深度先蒸发、价格随后动"的抢跑机会。
- **one-sided duration（单边挂盘时长）**：盘口只剩一侧有挂单（另一侧空了）这个状态持续了多久。短=转瞬即逝（可以去 fade），长=会持续（去 fade 会被抬走）。
- **matched control（配对对照）**：给每个事件配一个"同类但当时很安静（quiet anchor，过去 1 秒盘口顶部没变过）"的样本，用来做差分：事件结果 − 对照结果，去掉一天里时段本身的影响。
- **differential estimand（差分估计量）**：不是看绝对水平，而是看"事件 − 配对对照"的差，比原始水平更抗"时段假象"。
- **SMD（标准化均值差 standardized mean difference）**：衡量事件组和对照组某个协变量差多少，经验阈值 |SMD|>0.1 就算没配平。
- **HHI（集中度 Herfindahl 指数）**：对照样本是不是被少数几个市场垄断。越小越分散越健康。
- **TTL-capped dwell（TTL 封顶的停留时间）**：状态停留时间不是真实"市场开着多久"，而是"下一条更新来之前、最多记一个 TTL（主用 1 秒）"的封顶值；若下一条几小时后才来，只记作 1 秒并标 `RIGHT_CENSORED_STALE_TTL`（右删失）。**所以任何"深度挂了多久"都不能当市场在线时长读。**
- **sign-stability（符号稳定性）**：同一个信号在 3 个干净日、以及跨价格档，方向是否一致。方向翻来翻去=噪声=否掉；一致+单调=活下来。
- **right-censored（右删失）**：观测被窗口/TTL 截断了，真实值只会更长——所以被截断的尾部分位数（比如 p95=60.000s）不能当真实分布读。

---

## 存活候选（ranked）

### C1 —「深度回补做市」refill-timed requoting  ·  机制2（在深度可靠回补处提供流动性）  ·  ✅ SURVIVES（第一顺位）

**信号/数字（来源：`FULLSCOPE_L2_REPORT_TABLES.json.refill_hazard`，30 行 = 3 天 × 10 个 100ms 桶）**

- **首个 100ms 回补概率 refill_hazard_0-100ms = 24.5% / 29.3% / 30.8%（07-12 / 07-15 / 07-17）**
  - [是什么] 顶部深度被吃穿≥50% 后，头 100 毫秒内深度补回≥80% 的概率
  - [单位] 概率（0–1）
  - [怎么读] 越高 = 流动性回补越快 = 在这里做市越安全
  - [为什么] 决定我们敢报多紧的价差、被吃一半后要不要立刻撤
  - 字段：`pooled_interval_hazard`（horizon 0→100000us 那一行，逐日）
- **累计 1 秒回补概率 cumulative_refill_P_1s = 57.2% / 50.5% / 52.0%**（由逐桶 hazard 用 product-limit 生存曲线累出；输入字段同上）
  - [是什么] 被吃穿后 1 秒内深度补回来的总概率
  - [单位] 概率
  - [怎么读] 越高越安全；三天都在半数上下
  - [为什么] 定义"撤单/重挂"的时间预算——大约一半的抽深在 1 秒内自愈
- **回补主要发生在头 200ms**：累计 200ms = 43.8% / 37.3% / 39.2%（后面每桶 hazard 掉到 2–3%）。说明"要么很快补回、要么长时间不补"。
- **样本量**：首桶 at-risk 事件数 `at_risk_n` = 266,459 / 308,209 / 278,626；`events_n`（真回补数）= 65,312 / 90,383 / 85,850；每日 `strata` 288–356 个分层；atlas 口径 `REFILL_HAZARD` 代表 2,663,119 行。**n 很大，不是几个市场撑起来的。**

**符号稳定性：CONSISTENT。** 三个干净日方向一致、hazard 曲线形状一致（首桶最高、单调衰减）、量级贴近（首 100ms 24–31%，1 秒 50–57%）。这是全篇最稳的信号。

**诚实警告（thinness / 口径）：**
- **这是"显示深度"回补，不是"你自己的挂单成交/排队位置"**（`limitations`: "Displayed depth/refill is not own-order queue position or fill probability"）。深度补回来 ≠ 你那张单还活着、≠ 你成交。
- 分层里 `min_stratum_survival` 有时到 0.0（有些分层永不回补），说明池化数字盖住了尾部分层的异质性；**逐价格档的 hazard 这版表没给**，只给了池化值。
- 只有 3 天。

**可执行规则草稿（参数 TO-BE-FROZEN）：**
- 进场/挂单：在被跟踪的高流动市场，检测到同侧顶部深度被吃穿≥50%（沿用 `min_depletion_fraction=0.5` 定义）时，**不撤、在同价或紧邻价补挂**，赌 refill。
- 退出/撤单：给一个 τ≈**200–300ms** 的回补预算（因为累计回补的大头在头 200ms）；到 τ 仍未见深度回补则撤，避免落进"长时间不补"的坏分层。
- 门槛：只在逐价格档 refill_hazard 高的档位开（待验证门补出逐档表后冻结）。

**≥20 天验证要证/证伪什么：**
1. 逐价格档 [1,5)…[99,100) 的 refill hazard 是否单调、方向是否稳（这版只有池化值）。
2. 把"显示深度回补"升级成"**自有挂单在队列里的存活/成交**"——回补≠成交，这一步不做，机制2 只是环境描述不是边缘。
3. 20+ 天里首 100ms 24–31% 这个带是否守得住。

---

### C2 —「高流动性运动的价差捕获」cross-sectional spread capture  ·  机制1（价差捕获 + 逆向选择规避）  ·  ✅ SURVIVES（有条件：费后待定）

**信号/数字（来源：`RESULTS.json.base_methods[D3-B01-MARKOUT].summary`，111 行 = sport×phase×horizon）**

- **成交后漂移 signed_markout（log-odds）随时间放大**：n 加权 = **0.038（1s）→ 0.046（5s）→ 0.056（30s）**
  - [是什么] 一笔成交后，中间价往主动方向又走了多少（log-odds）
  - [单位] log-odds（≈0.038 → 近 50¢ 时约 1¢）
  - [怎么读] 为正=价格继续朝吃单方跑=挂单方被逆选/抬走；数越大越"被抬得远"
  - [为什么] 这是做市的逆向选择成本；必须小于你收的半价差才有毛边缘
  - 字段：`mean_signed_markout_logodds`（按 `horizon_us` 分组，n=`n` 加权）
- **半价差普遍大于逆选漂移 → 毛口径做市边缘为正**。1 秒、高流动性大票的"margin = half-spread − markout（log-odds）"：
  - Soccer（UNKNOWN_PHASE）：markout 0.025 / half-spread 0.069 / **margin +0.045**，n=6,833,286 笔、3,263 个市场
  - Tennis：0.045 / 0.086 / **+0.041**，n=6,536,141、6,094 市场
  - Baseball：0.046 / 0.098 / **+0.051**，n=2,395,922、6,914 市场
  - Basketball：0.048 / 0.087 / **+0.040**，n=3,094,371、4,072 市场
  - 字段：`mean_signed_markout_logodds`、`mean_spread_logodds`（half=÷2）、`n`、`markets`
  - [怎么读 margin] 为正=毛口径上半价差吃得住逆选=可能有做市空间；这是**毛口径、未扣费**
- **横截面一致**：1 秒 horizon 下 37 个 cell 里 **36 个 markout 为正**；所有 n>1000 的 cell margin 都为正（负 margin 只出现在 n<80 的噪声格）。且 markout 随 horizon 单调增（0.038→0.046→0.056）。

**符号稳定性：横截面 CONSISTENT，逐日 UNVERIFIABLE。** B01 是按 sport×phase×horizon 汇总、**跨 7–8 天池化的，没有逐日字段**（`summary` 无 date 列），所以无法按 07-12/15/17 单独验符号。它的稳定性证据是"36/37 格同号 + 随 horizon 单调"，属于横截面稳，不是逐日稳。

**诚实警告：**
- **毛口径。deep01 已判 S1 纯做市费后负期望。** 粗算：margin≈0.04 log-odds ≈ 1¢（近 50¢，×0.25）；Kalshi 手续费在 50¢ 附近约 1.7¢/张量级 → **中间价档手续费很可能吃掉这 1¢ 毛边缘**。所以 C2 不是现成边缘，只有在"markout 低 + 价差够宽到盖过费"的价格档才可能翻正。
- 无逐日、无逐价格档拆分（`summary` 只到 sport/phase）。

**可执行规则草稿（参数 TO-BE-FROZEN）：**
- 只在高流动性大票（Soccer/Tennis/Baseball/Basketball）双边报价；
- 报价前先看该 cell 的 markout/half-spread 比：只在 **half-spread − markout − 双边手续费 > 0** 的价格档挂；
- 逆选保护：成交被打后若 markout 方向继续（价格朝对手方向走），按 30s markout 单调增（0.056>0.038）说明会越亏越多，**立刻对冲/平**而不是扛。

**≥20 天验证要证/证伪什么：**
1. **逐价格档 [1,5)…[99,100) 的 markout 与 half-spread**：找出费后 margin>0 的档（这版只到 sport 级，找不到档）。
2. 把 log-odds margin 换成**费后每张净分**（带 Kalshi 真实费公式 + 悲观成交界）——这是 go/no-go。
3. 逐日复核 07-12/15/17 及更多天，确认 36/37 同号不是池化假象。

---

### C3 —「赛前转瞬单边挂盘的 fade」pre-start transient one-sided fade  ·  机制3（消退单边失衡）  ·  ⚠️ MARGINAL（薄，仅窄口径存活）

**信号/数字（来源：`RESULTS.json.base_methods[D3-B02-ONESIDE].summary`，82 行）**

- **赛前单边状态才真的短命**：Tennis `PRE_SCHEDULED_START` 单边时长 **p50 = 1.001s，p95 = 30.999s**；Basketball PRE p50 = 5.001s。
  - [是什么] 盘口只剩一侧、这个状态持续多久
  - [单位] 秒
  - [怎么读] 短=转瞬即逝、失衡会自愈=可以站对面挣回归；长=会持续=站对面被抬走
  - [为什么] 决定 fade 单边失衡到底安不安全
  - 字段：`p50_duration_seconds` / `p95_duration_seconds`，`book_state='ONE_SIDED'`，`phase='PRE_SCHEDULED_START'`

**符号稳定性：逐日 UNVERIFIABLE（B02 也是跨天池化，无 date 列）。**

**诚实警告（这是它只算 MARGINAL 的原因）：**
- **薄**：Tennis PRE 只有 `intervals`=18,387、`markets`=1,248、`days`=6；Basketball PRE `days`=6、intervals=7,981。
- 只有赛前窄口径成立；见下方 R1，广义 fade 被否。

**可执行规则草稿（TO-BE-FROZEN）：** 仅在赛前时段（PRE_SCHEDULED_START）、Tennis/Basketball，检测到单边挂盘出现时，站到空的一侧小额试挂，赌 1–5 秒内双边恢复；超过 p95（~31s）未恢复即撤。

**≥20 天验证要证/证伪什么：** 赛前单边 p50 是否稳定在 1–5s；把"单边状态恢复"接到"实际回归 PnL/成交"；把样本从 6 天堆到 20+ 天看 markets 数是否够。

---

## 被否 / 噪声（REJECTED）

### R1 —「广义 fade 单边失衡」broad one-sided fade  ·  机制3  ·  ❌ REJECT
- **单边状态不是转瞬即逝，而是持续的**：大票 Golf/Baseball/Soccer/Basketball（UNKNOWN_PHASE）单边 **p50 = 8–11 秒**，且 **p95 几乎全部正好 = 60.000s**（来源：`base_methods[D3-B02-ONESIDE].summary.p95_duration_seconds`）。
  - p95=60.000 是**观测窗/删失上限，不是真实分位数**（right-censored，见术语表），尾部不可读。
- 单边状态占观测态时长 **16.9%**（one-sided 174,820,172s / 总 1,036,792,251s，字段 `exposure_seconds`）——不算罕见，但**持续时间太长**，站对面 fade 会被抬走。
- 结论：广义"失衡就 fade"证据反向（会被run over），否掉。只保留 C3 赛前窄口径。

### R2 —「深度蒸发抢跑」depth-evaporation front-running  ·  机制4  ·  ❌ NOT-ESTIMABLE-FROM-DELIVERED（暂缓）
- 事件是定义好、也抽出来了：`episode_rows` = **853,294**，retreat 定义在 `stage_abi.top3_retreat_definition`（撤≥50% 最优3档、≥1¢），atlas `RETREAT_TOP3_BASELINE` 代表 206,551 行。
- **但机制4 要的"差分结果"（撤退事件后价格走多远 vs 配对对照）在交付的这几张表里根本没有**——只给了配对**覆盖率**，没给事件→价格移动的 outcome 表。
- 而且配对覆盖极薄：**match_rate = 1.9% / 1.9% / 1.6%**（07-12/15/17），matched = 5,072 / 5,882 / 4,527，占各自 ~266k/308k/278k 事件的不到 2%（来源：`FULLSCOPE_L2_REPORT_TABLES.json.match_coverage`）。
  - 另外配对在**深度上没配平**：`smd_depth3_e4` = -0.32 / -0.31 / -0.40（|SMD|>0.1 即失衡，深度这一维超标；spread 和 imbalance 的 SMD 都 <0.08 配平了）。→ 现有对照有深度残余混杂。
- 结论：交付物里估不出机制4 的边缘。**暂缓**，等验证门补出"事件-对照 outcome 差分表" + 20+ 天把 matched 数堆上去 + 修深度配平。

### R3 —「跨市场残差/合成套利」linked-market residual  ·  机制外（B03）  ·  ❌ NOT_ESTIMABLE
- `base_methods[D3-B03-XMKT].status = NOT_ESTIMABLE`，`summary` = 0 行。瀑布：28,080 个候选 → 过不了 `payout_exhaustiveness_authenticated`（0 存活）→ 过不了 `fee_and_fill_feasible`（0 存活）（来源：`EXCLUSION_WATERFALL.json`）。
- 原因（`reason`）：event_ticker 相邻不证明赔付互斥；没有每条腿的费和严格多腿成交，残差就是个"合成套利"假声。否掉。

### 附注 — B04-RHYTHM（活动节律）不是候选
- `base_methods[D3-B04-RHYTHM]`（4,463 行，按 utc_hour×sport×phase 的 `trades_per_active_market_minute`）是**产能/择时**输入，不是独立边缘。留作执行择时的背景，不进候选。

---

## TOP PICKS（进验证门的顺序）

1. **C1 深度回补做市（机制2）— 第一个进门。** 一行签名：**被吃穿后 1 秒内显示深度回补概率 50–57%、头 100ms 就 24–31%，三个干净日方向与量级全一致（refill_hazard，n=27–31万事件/日）。** 理由：三天符号最稳、样本最大、机制最干净。唯一硬门槛是把"显示深度回补"证成"自有挂单成交"——这正是验证门该做的第一件事。
2. **C2 高流动性价差捕获（机制1）— 第二，带费后闸门。** 一行签名：**1 秒 half-spread(0.07–0.10) 普遍大于逆选 markout(0.025–0.05)，36/37 个 sport 格毛边缘为正（B01，n=2,290 万笔/1s）**；但毛口径、deep01 已判纯做市费后为负，进门第一步就是逐价格档换算费后每张净分。
3. （备选）**C3 赛前转瞬单边 fade（机制3 窄口径）**——只在证明 C1/C2 后、且样本堆到 20+ 天再碰，现在太薄。

**机制4（抢跑）与 B03（跨市场）现在交付物里估不出，不进门，等补表。** 这是诚实的 null，不是失败。

---

*生成于 D3-W2A run；run_id 见 `RESULTS.json.run_id`。source_binding sha256 = `c08083fe…36b1979`（L2 表）。全篇 EXPLORATORY_ONLY / NOT_STRICT_ACCEPTANCE / candidate_or_profit_claim=False。*
