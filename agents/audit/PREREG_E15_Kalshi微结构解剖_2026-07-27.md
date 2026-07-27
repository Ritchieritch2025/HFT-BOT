# 预注册 · E15 · Kalshi 微结构解剖(Dubach 2026 复现 + 探索)

> **性质:A 类研究。只读封存数据,零实盘,零交易规则产出。**
> **所有结论标注「结构描述,样本内」。任何读数不得直接进入引擎参数(反例 13)。**
> **本文件在任何统计量被计算之前写死。** 写死时刻已经跑过的只有「覆盖率探针」
> (行数、市场数、字段可用性、时间间隔中位数),探针输出见 §9,**不含任何结果变量**。

| 项 | 值 |
|---|---|
| 实验号 | E15(`docs/experiments/LEDGER.md`) |
| 源论文 | Dubach, P. D. (2026-05-15), *The Anatomy of a Decentralized Prediction Market: Microstructure Evidence from the Polymarket Order Book*, arXiv:2604.24366v2 — `/Applications/Research Ritch/The Anatomy of a Decentralized Prediction Market- Microstructure Evidence from the Polymarket Order Book.pdf` |
| 复现范围 | SF1(点差-价位)、SF2(深度形状)、SF5(分类有效价差)、SF8(深度-临近结算) |
| 明确不复现 | SF3(Polygon 区块时钟)、SF4(做市钱包 HHI)、SF6(采集延迟)、SF7(自成交洗盘)——链上/钱包身份在 Kalshi 不存在,或已由 E8 延迟 census 覆盖 |
| 代码 SHA(预注册时刻) | `2bb6a3cc706a` |
| 分层规则版本 | `tools/research/replication/taxonomy.py` sha256 前 16 位 `91dd1d4cdd7fe40b` |
| catalog 快照 | `snapshot=20260724T213017Z`(3,303 series shard,唯一 DONE 快照) |
| 预注册时刻 | 2026-07-27(操作员指令当日) |

---

## 1 · 为什么做,以及不打算得到什么

Dubach 的八条 stylized facts 是在**周-月级存续的 Polymarket 市场**上测的。我们的主战场是
**15 分钟生命周期的二元合约**。所以这次不是「对表」:对表只是把 PM 锚点放在同一张表的
一列上,让读者知道我们的数量级站在哪。**真正的产出是 PM 测不了的那些切面**——
生命周期内的价差演化、YES/NO 两侧书的对称性、行权价距离上的深度形状、
以及**市场内**(而非横截面)的深度-临近结算轨迹(PM 第 5.8 节明确把这个问题推迟了)。

**本实验不产生任何交易规则。** 样本内形状不得当先验入引擎(反例 13);
即使某个格子看起来「肥」,它进入引擎的唯一通道是独立的样本外实验。

---

## 2 · 数据与样本

| 用途 | 数据集 | 窗口 |
|---|---|---|
| 报价/点差/tick 绑定(SF1-K)、有效价差 mid 基准(SF5-K)、行权价阶梯深度(SF2-K 加切面 ②) | `facts/orderbooks_l1`(~1 Hz 顶档采样,探针实测中位间隔 1.002 s) | 全部封存日 **2026-07-10 .. 07-24** |
| 成交(SF5-K) | `facts/trades`(firehose 全交易所) | 同上 |
| 十档深度形状(SF2-K)、市场内深度面板(SF8-K 主口径) | `facts/orderbooks_full`(增量+快照,需重建) | **干净三日 07-12 / 07-15 / 07-17** |
| 结算/收盘真值 | catalog_normal `snapshot=20260724T213017Z` | — |

**窗口右端 07-24 而非 07-25/26**:唯一 DONE 的 catalog 快照拍摄于 2026-07-24T21:30:17Z,
之后收盘的市场在结算索引中不存在。**快照截止过滤 = 只保留 `close_time ≤ 2026-07-24T21:30:17Z`
且 status=finalized、result∈{yes,no} 的市场**。覆盖损失在报告的「join 账」里逐族披露。

### 2.1 样本单位与置信区间

- **样本单位 = 市场**(反例 3:按笔数算 n → CI 虚窄 7 倍)。
- 所有 CI = **市场聚类 bootstrap × 1000**,`stats.cluster_bootstrap_ci`,seed **20260727**,双侧 95%。
- 「族中位数」= 先算每市场一个数,再在市场间取中位数。跨族**永不合并**(操作员 07-25 裁决)。

### 2.2 入选门槛(先于数据写死)

| 表 | 市场入选条件 |
|---|---|
| SF1-K | 该市场在窗口内有 ≥ 30 条**两侧齐全**的 L1 报价秒 |
| SF2-K | 该市场通过 §5 的重建验收门,且 ≥ 30 个 10 秒采样点 |
| SF5-K | 该市场有 ≥ 10 笔可 as-of 匹配到 mid 的成交 |
| SF8-K | 同 SF2-K,且 tte > 0 的采样点 ≥ 30 |

### 2.3 分层口径四件套(反例 15)

随报告存档:①分类规则版本号(`taxonomy.py` sha256);②catalog 快照哈希;
③**逐 ticker 归属表**(market_ticker → family,parquet);④**未识别清单**
(`family_of` 返回 None 的 series × 行数,含 Exotics 等 out-of-scope 类目)。
缺任一件,报告作废。

---

## 3 · 口径定义(逐字写死)

**单位**:`*_e4` = 1e-4 美元;**¢ = e4/100**;`count_e4` = 张数 × 1e4;`ts_utc` = µs UTC。
YES 轴报价:`mid_c = (yes_bid_e4 + yes_ask_e4) / 200`,`spread_c = (yes_ask_e4 − yes_bid_e4)/100`。
bps 一律相对 mid:`spread_bps = 10000 × spread_c / mid_c`(与 PM 同轴,PM 报的是**全价差** bps)。

- **两侧齐全**:`yes_bid_e4 > 0 且 yes_ask_e4 < 10000`。否则记为**单边/空书**,
  不进点差统计,但**单独报「空书时间占比」**(PM 没有这个读数,Kalshi 墓地区有)。
- **停留时间(dwell)**:第 i 行的权重 = `min(下一行 ts − 本行 ts, 60 s)`,
  末行权重 = `min(close_time − ts, 60 s)`。60 s 上限先于数据写死,理由:1 Hz 流上的
  长间隔代表订阅中断而非真实书状态。**无上限版本作为敏感性列一并输出。**
- **tick**:市场级判定——窗口内任一 L1 报价价格不是 100 e4 的整数倍 ⟹ tick = 0.1¢;
  否则 tick = 1¢。(探针已确认 crypto 走 0.1¢、体育走 1¢,但判定按市场逐一做。)
- **tick 绑定率**:`spread_c == tick` 的 **dwell 占两侧齐全 dwell 的比例**。
- **tte**:`(close_time − ts) / 60 s`,分段沿用 `taxonomy.BANDS` 的 Kalshi 版:
  `>10m / 5-10m / 2-5m / <2m`(SF1-K 加切面按操作员指定的四段)。
- **mid 十分位**:每市场的 **dwell 加权平均 mid**,按 PM Table 1 的**固定十等分**
  `[0,10) [10,20) … [90,100]` ¢ 分箱(不是分位数分箱),以便与 PM 逐格对照。

### 3.1 SF5-K 有效半价差

`eff_half_c = dir × (yes_price_c − mid_c)`,其中 `dir = +1 当 taker_side='yes'`,`−1 当 'no'`。
理由:成交表的 `yes_price_e4` 已是该笔成交的 YES 轴价格(yes+no ≡ 10000),
所以「taker 买 NO」等价于「taker 卖 YES」,方向取负。
**`taker_side` 是交易所真值,不是 Lee-Ready 推断**——这正是 PM 全文最大的痛点
(他们的 feed 推断只有 ~59% 与链上真值一致,导致 67% 的市场有效价差**变号**)。
我们把这一点作为**方法论对照**写进报告,而不是复现他们的误差。

`mid_c` = 成交时刻**严格之前**最近一条两侧齐全的 L1 报价;陈旧上限 **60 s**,超时该笔丢弃。

### 3.2 SF2-K 深度形状

十档:从最优价向内数 1..10 档。**买侧 = YES 书的 yes 买盘;卖侧 = NO 买盘镜像**
(`yes_ask = 100 − no_bid`),两侧都是真实挂单。
每档份额 = **先在市场内对时间取平均,再做比值**(与 PM 一致):
`share_k = mean_t(depth_k) / mean_t(Σ_{j≤10} depth_j)`。
形状统计量:KL(share ‖ 均匀 0.1);top-heavy = `share_1 > 0.5`;`share_1 ∈ [0.05,0.20]` 占比。
采样网格:**10 秒**,取该秒最后一个书状态。

### 3.3 SF8-K 市场内面板

```
log(depth_top10_t) = α_i + β1·log(tte_s) + β2·log(p(1−p)) + β3·log(1 + cum_contracts_t) + ε
```
`α_i` = 市场固定效应(组内去均值),SE = **市场聚类**三明治估计。
`p` = 该采样点 mid 的概率(mid_c/100),`cum_contracts_t` = 该市场到 t 为止的累计成交张数
(由成交表 10 秒桶累加)。规格阶梯照 PM 的顺序报四行:
①双变量 ②+族固定效应 ③+log 成交量 ④全规格(市场固定效应版)。
**判据(先于数据写死)**:全规格下 β1 的 95% CI 含 0 ⟹「墓地纯是流的问题」;
CI 排除 0 ⟹「深度撤退是独立通道」。同一回归输出 **tte 的增量 R²**
(全规格 R² − 去掉 log tte 的 R²)作为 **E13「秒数分桶退役」的压缩验收读数**。

---

## 4 · 四张表 / 四张图(交付形状)

| # | 表 | 图 | PM 对照锚点 |
|---|---|---|---|
| SF1-K | 十分位 × (中位价差 ¢/bps、IQR、tick 绑定率、空书占比) + tte 四段再切一层 | 价差 bps vs 十分位,IQR 带,PM 曲线叠加;crypto_15m 的 tte 分线 | 中间档 **400 bps**;<0.10 档 **1,300–1,818 bps** |
| SF2-K | L1..L10 份额(中位/IQR/p10-p90)分族,买/卖侧分列 | 份额 vs 档位,均匀零线 0.10,PM 中位线叠加 | **L1 份额中位 0.136**;top-heavy **9%**;KL 中位 **0.087** |
| SF5-K | 分族有效半价差(¢/bps,中位/IQR/聚类 CI)‖ WHO_PROFITS maker 毛边际 → 差 = 逆向选择耗损 | 分族条形 + 误差棒,并排耗损列 | PM 分类中位数 −0.039..+0.008 概率点(**含 59% 符号噪声**) |
| SF8-K | 四规格 × (β1、聚类 SE、CI、R²、tte 增量 R²) | 系数阶梯 + 95% CI | PM 横截面:0.818 → 0.008(加 duration/p(1−p)/volume 后塌到零) |

---

## 5 · 已知答案门(强制,C1 教训)

L2 增量重建**必须**先过 known-answer 门,否则整轮作废(grid_replay v1 打出 199,482 笔
假成交的教训)。门的定义:

1. 每个市场从**第一条非空 snapshot** 起重建;之前的增量丢弃(探针:crypto 24/24、
   tennis 204/215 的首行即 snapshot)。
2. 重建出的最优买/卖价,与 `orderbooks_l1` 同市场、同秒的 `yes_bid_e4/yes_ask_e4`
   逐点比对。**要求逐市场一致率 ≥ 99%**;低于阈值的市场**整体剔除**并计数。
3. 任一档位出现**负数量** ⟹ 该市场剔除并计数。
4. 门的通过率、剔除清单写进报告方法页。**门不过就不报 SF2-K/SF8-K。**

---

## 6 · 意外清单(强制产出)

任何满足以下之一的读数**单列一行**:
①与 PM 锚点**方向相反**;②与 E11 / E13 / E14 / WHO_PROFITS 已有结论**冲突**;
③族间差异**超预期一个量级**;④任何「看了觉得不对劲」的分布形状。

每行必须给:**读数 / 对照物 / 初步猜测 / 值不值得立独立实验**。
**没有意外也要明写「无」。** 意外清单不是彩蛋,是这次实验的主要产出通道。

---

## 7 · 禁止事项

1. 不产生交易规则、不改任何引擎参数、不碰实盘;
2. 不把样本内形状当先验接线(反例 13);
3. 不跨族合并;不按笔数算 n;
4. 不把「市场平均」当自家 EV(反例 14);
5. 不向 warehouse 写入任何文件;EC2 上只在 `/home/ubuntu/e15_scratch` 落小工件。

---

## 8 · 运行方式

```bash
rsync -az tools/research/replication/ ubuntu@EC2:/home/ubuntu/e15_scratch/replication/
ssh EC2 'cd /home/ubuntu/e15_scratch && nice -n 19 ionice -c3 \
    /home/ubuntu/hft-bot/.venv/bin/python replication/experiments/microstructure_e15.py all'
scp EC2:/home/ubuntu/e15_scratch/out/*.parquet .
python3 .../microstructure_e15_analyze.py <artifact dir> results.json
REPORT_PNG=png python3 .../microstructure_e15_report.py results.json out.pdf meta.json
```

**EC2 纪律**:`nice -n 19 ionice -c3`,DuckDB threads=2 / memory_limit=8GB,
`temp_directory=/dev/shm/e15tmp`(**根盘只剩 2.0 GB,严禁磁盘 spill**)。

---

## 9 · 预注册前已跑过的探针(全部为覆盖率,无结果变量)

- 封存日清单 07-10..07-26;L1 分类目字节数;L1 `record_class` 只有 `A`;
- L1 行间隔中位数 1.002 s(KXBTC15M)/ 1.574 s(Tennis)⟹ ~1 Hz 顶档流;
- L2 三日覆盖:仅 Crypto/BTC + Sports(Baseball/Basketball/Esports/Soccer/Tennis/Golf),
  **L2 是配额抽样**(crypto 每小时约 1 个 15M 市场,07-15 共 24 个;tennis 215 个);
- L2 每市场首行即 snapshot(crypto 24/24;tennis 204/215,9 个无非空 snapshot);
- crypto 报价存在 **0.1¢ deci-cent** 档位,体育为 1¢;
- KXBTC15M **每事件仅 1 个市场**(无行权价阶梯)⟹ 「距行权价距离」在 15M 上
  只能用 **|mid − 50¢| 作市场隐含在值程度代理**;而 KXBTCD/KXBTC(小时/日)
  **每事件约 180 个行权价**,阶梯真实存在 ⟹ 加切面 ② 用 L1 顶档深度在小时盘阶梯上做,
  行权价从 ticker 解析,隐含现货由阶梯上 mid 穿越 50¢ 的位置线性插值得到。
- catalog 唯一 DONE 快照 = 20260724T213017Z。

**以上探针不触碰任何 §3 定义的结果变量。**

---

## 10 · 判决模板(报告必须回答的四句话)

1. SF1-K:Kalshi 的价差-价位曲线与 PM 同向还是反向?15 分钟生命周期内它怎么走?
2. SF2-K:我们的书是 top-heavy 还是均匀铺开?YES/NO 两侧对称吗?
3. SF5-K:哪一族的逆向选择耗损最大?与 WHO_PROFITS 的 maker 毛边际相减后还剩什么?
4. SF8-K:控制 p(1−p) 与成交量之后,tte 还是不是独立通道?(E13 压缩验收)
