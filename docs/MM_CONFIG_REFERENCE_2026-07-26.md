# Crypto MM 全策略参数手册(2026-07-26)

调参对照表。每个参数标注:设置位置(env=环境变量 / ctrl=控制文件热改 / code=代码常数)、
默认值、金丝雀值、证据。**带🔒的是硬顶或冻结值,改它需要走单独流程,不是调参。**

调参纪律:参数搜索只在回放TRAIN段(07-12..19);VALIDATE(07-20..23)每个配置只许验一次;
对同一前向窗口调了参再测 = 禁止(审计红线)。

---

## 1. 主线策略(公允价maker,MM_PAIR=0)

### 1.1 定价与入场

| 参数 | 位置 | 默认 | 金丝雀 | 说明与证据 |
|---|---|---|---|---|
| MM_MARGIN | env | 0.3¢ | **2.5¢** | 最小edge。**证据:bid侧成交后5s markout −1.76¢,0.3¢结构性倒挂**。调参空间:2.0–3.5,按markout+费+目标利润定 |
| MM_GAMMA | env | 0.5¢/张 | 0.5 | 库存偏移:每持1张风险合约,累积方向退0.5¢。回放skew臂(0.2/0.5/1.0)今晚出对照数字 |
| sentinel阈值 | code | 15¢ | — | \|fair−mid\|>15¢暂停开仓。批评#3认为太钝(13h触发568次);改小需引擎代码改动(codex工单) |
| zone门槛 🔒 | code | 120/300/600s | — | <120禁入;120-300只做<20¢/>80¢;300-600需\|mid−50\|>10。**6.1亿张结算真值实测**(<120s区−3.5¢/张)。注意:尾部区暴露于跳跃风险(\|z\|>4为正态224倍),建议金丝雀期间观察尾部区成交的markout再决定是否收紧 |
| 0.1¢改善区 | code | <10¢或>90¢ | — | 极端区尝试+0.1¢价格改善,其余整分 |

### 1.2 仓位与资金

| 参数 | 位置 | 默认 | 金丝雀 | 硬顶🔒 | 说明 |
|---|---|---|---|---|---|
| MM_CLIP / ctrl.clip | env+ctrl | 5.00张 | **1.00** | 20 | 单笔下单量。批评#6:放量时用2-3(留库存渐进空间),别用5 |
| MM_MAX_NET / ctrl.max_net | env+ctrl | 6 | **1** | 100 | 净仓上限,下单前投影检查 |
| MM_MAX_COST / ctrl.max_open_cost | env+ctrl | $50 | **$5** | $200 | 敞口=resting+pending+已成交未释放 |
| 一边成交锁一边(闩锁) | code | 开 | 开 | — | 非配对模式防单边堆积;回放refill臂在测"解锁+库存帽"替代方案 |

### 1.3 报价节奏

| 参数 | 位置 | 默认 | 说明 |
|---|---|---|---|
| MM_MIN_QUOTE_AGE_S | env | 15s | 新单最短保留。批评#3:快市中是stale-quote风险源,σ挂钩快撤在工单里 |
| MM_MIN_REQUOTE | env | 0.5¢ | 目标价变化<0.5¢不撤单(F5证据) |
| 每侧改单节流 | code | ~1s | |
| 令牌桶 | code | 8爆发/4每秒 | API限速 |

### 1.4 数据质量闸(动这些=改变"何时敢报价")

| 参数 | 位置 | 默认 | 说明 |
|---|---|---|---|
| MM_RTI_SHORT/LONG_WINDOW_S | env | 60/300 | 双窗σ取max。方差比检验:√t缩放实测1.03–1.12×,σ量级OK |
| MM_RTI_MIN_COVERAGE | env | 0.999 | 300s窗口须99.9%连续 |
| MM_RTI_MAX_GAP_S | env | 1.5s | tick间隙>1.5s拒用 |
| MM_RTI_MAX_SOURCE_AGE_S | env | 2.0s | 源数据超龄停报 |
| MM_CF_CAPTURE_GLOB | env | EC2路径 | 重启热身用的历史tick |

### 1.5 停机闸(建议只朝更紧方向调)

| 参数 | 位置 | 默认 | 说明 |
|---|---|---|---|
| MM_ECON_HALT | env | −$2 | 已实现亏损熔断,**实际最先触发的闸** |
| 连续零收入结算 | code | 2次 | 熔断,不自动恢复 |
| MM_KILL_LOSS | env | −$50 | 总杀线 |
| MM_RECON_MAX_DIVERGENCE | env | 0张 | 本地vs交易所仓位差,零容忍 |
| insufficient_balance | code 🔒 | 一击停 | |
| 歧义处理(超时/404/回执不符) | code 🔒 | 全部HALT | |
| MM_REQUIRE_FLAT_START | env | 0(金丝雀1) | 启动须平仓 |
| MM_ONLY_TICKER | env | 空 | 单市场白名单 |

---

## 2. 配对策略(MM_PAIR=1,等回放判决)

| 参数 | 位置 | 默认 | 说明与证据 |
|---|---|---|---|
| MM_PAIR | env | 0 | 总开关。影子13h:自然配对+1.11¢×536,孤腿−12.60¢×195,净−2.55¢/周期 |
| MM_PAIR_LOCK_C | env | 99¢ | 两腿成本天花板(99锁≥1¢)。回放98/99对照臂在跑 |
| MM_PAIR_MIN_EDGE_C | env | 0.5¢ | 98.9¢的对拒绝。批评#6:98.5¢机会多为stale quote,admit_gap臂在验 |
| MM_UNPAIRED_MAX_CT | env | 2张 | 每边孤腿上限。**约束:CLIP≤此值**(配对模式配置检查) |
| MM_UNPAIRED_AGE_S | env | 90s | 孤腿taker强平年龄。**当前影子60s版:194/195次强平都卡在这个闹钟上,单均−12.6¢——这是全部亏损来源**。回放30/90/off三臂+新mkexit臂就是在调它 |
| MM_PAIR_MAX_LOCKED_COST | env | 0.25×MAX_COST | 配对锁资上限 |
| MM_MAX_CYCLES | env | 0(关) | 金丝雀:N个周期后自动停 |
| 完成率证据 | — | — | 按入场价:<20¢区64.4%,>80¢区81.5%;盈亏平衡需91.9%(或孤腿成本<3¢) |

## 3. 预报价PREQUOTE(实盘禁止,影子/回放专用)

| 参数 | 位置 | 默认 | 说明 |
|---|---|---|---|
| MM_PAIR_PREQUOTE | env | 0 | 两腿全能挂才挂。**代码强制禁live** |
| MM_PAIR_MIN_DEPTH_CT | env | 1081.01 | 深度准入,默认值=封死 |
| MM_SHADOW_QUEUE_SIM | env | 0 | 悲观排队成交模拟,仅shadow |

## 4. 回放网格臂参数(grid_replay_v2,调参主战场)

Arm字段(每臂一组合,当前2中性+26网格臂):

| 字段 | 取值集 | 对应实盘参数 |
|---|---|---|
| mode | tail/front | 排队尾 vs +0.1¢改善 |
| refill | T/F | 闩锁 vs 成交后重新武装 |
| cap | 5/10/999 | 库存帽(配refill) |
| gamma_c | 0.2/0.5/1.0 | → MM_GAMMA |
| requote_min_c | 0/0.5 | → MM_MIN_REQUOTE |
| pair_lock_c | 98/99 | → MM_PAIR_LOCK_C |
| unpaired_max_lots | 1/2 | → MM_UNPAIRED_MAX_CT |
| flatten_age_s | 30/90/120/180/1e9 | → MM_UNPAIRED_AGE_S |
| maker_grace_s (codex新) | 30 | 硬砍前宽限 |
| min_opp_depth_ct (codex新) | 5 | 对面深度准入 |
| orphan_maker_age_s (新) | 30/60 | 孤腿maker退出起始年龄 |
| orphan_maker_max_loss_c (新) | 1/3 | maker退出容亏(限亏锁) |
| admit_pair_gap_c (新) | 1/2 | 反腿天花板与对面best最大距离 |

框架常数🔒:延迟60ms、CLIP=5张、报价窗10–30min、T−5m强撤、TRAIN=07-12..19、VAL=07-20..23。

## 5. 预算护栏(完整版引擎,codex收工后)

| 参数 | 位置 | 值 | 说明 |
|---|---|---|---|
| 锚/容亏/地板 🔒 | code | $20.157/$10/$10.157 | 改=新预算,需操作员单独建档 |
| MM_BUDGET_PATH | env | EC2路径 | 预算档案 |
| MM_BUDGET_ACCOUNTING_CONTRACT_VERIFIED | env | 0 | 探测证据闸,0=live拒绝启动 |

## 6. 待做的引擎参数(codex工单,规格见信箱20260726T1510)

操作员裁决(07-26):尾部区不关,便宜侧进攻、贵侧限制、σ快撤保命。

| 新参数 | 默认 | 说明 |
|---|---|---|
| MM_FAST_PULL / MM_PULL_K | 0 / 2.5 | 不利真值移动>max(入场edge, k·σ·√age)立即撤;优先于队列保护 |
| MM_TAIL_CHEAP_ONLY | 0 | 尾部区只挂便宜侧(做多凸性,亏损封顶=入场价,肥尾利好) |
| MM_TAIL_EXPENSIVE_MARGIN_C | 2.0¢ | 若保留贵侧,额外加价 |
| MM_MARGIN_BID / MM_MARGIN_NO | 回落MM_MARGIN | 侧向拆分(markout:bid −1.76¢ vs NO −0.12¢) |

- 日历化σ下限+事件时刻停报(整点/美股开盘/数据发布)——次优先
- 注意:尾部区(120–300s)不在回放报价窗(10–30min)内,#TAIL系改动只能影子验证
