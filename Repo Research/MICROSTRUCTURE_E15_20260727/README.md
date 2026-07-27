# E15 · Kalshi 微结构解剖 — Dubach (2026) 复现 + 探索

**交付物**:`MICROSTRUCTURE_E15_2026-07-27.pdf`(13 页)
**源论文**:*The Anatomy of a Decentralized Prediction Market: Microstructure Evidence from the
Polymarket Order Book*,Philipp D. Dubach,2026-05-15,arXiv:2604.24366v2 —
`/Applications/Research Ritch/The Anatomy of a Decentralized Prediction Market- Microstructure Evidence from the Polymarket Order Book.pdf`
(30B 事件 / 52 天的 Polymarket WebSocket 存档,600 市场预注册面板,八条 stylized facts)
**预注册**:`agents/audit/PREREG_E15_Kalshi微结构解剖_2026-07-27.md`(commit `caea460`,**先于任何统计量**)
**性质**:A 类只读研究。零实盘,零交易规则产出。所有结论标「结构描述,样本内」。

---

## 复现了哪四条,以及为什么只有四条

| Dubach | 我们 | 说明 |
|---|---|---|
| SF1 彩票价差溢价 | **SF1-K** ✅ | + Kalshi 独有:tte 四段切面、tick 绑定率、空书时间占比 |
| SF2 深度集中度 | **SF2-K** ✅ | + Kalshi 独有:买/卖侧分开、行权价阶梯上的距离切面 |
| SF5 分类有效价差 | **SF5-K** ✅ | 用 `taker_side` 交易所真值,不是 Lee-Ready 推断 |
| SF8 深度-临近结算 | **SF8-K** ✅ | **市场内面板**(PM 只做了横截面,并明确推迟了市场内那一问) |
| SF3 Polygon 区块时钟 | ❌ | 链上概念,Kalshi 没有区块 |
| SF4 做市钱包 HHI | ❌ | Kalshi 公开 tape 无交易者身份 |
| SF6 采集延迟 | ❌ | 已由 E8 延迟 census 覆盖,且那是我们管道的属性不是交易所的 |
| SF7 自成交洗盘 | ❌ | 需要钱包身份 |

---

## 四句判决

1. **SF1-K**:Kalshi 的价差-价位曲线**以「分」计是倒 U**(在值处最宽 10¢,两端 1-3¢);
   换成 bps 才长得像 PM 的「彩票溢价」——那是被 mid 做分母造出来的。
   crypto 15M 的 **230 bps 比 PM 最活跃的十分位(400 bps)还窄**。
2. **SF2-K**:书**不是 top-heavy**(强 top-heavy 1.4%,PM 9%),也不是均匀——
   **第二档比第一档还厚(0.193 vs 0.147)**,而 PM 的剖面从 L1 单调衰减。
   两侧不对称:买侧只占十档深度的 **38.8%**。
3. **SF5-K**:有效半价差减去 WHO_PROFITS 的 maker 毛边际 = 逆向选择耗损。
   crypto 15M **吐回 0.442¢(62%)**,crypto 小时盘吐回 0.301¢(39%)。
4. **SF8-K**:**深度撤退是独立通道**。市场内面板 log(tte) 系数 **+0.251,CI [+0.131, +0.372]**,
   排除 0;PM 的横截面同规格塌到 +0.008。**但分族拆开后 crypto 15M 的 CI 含 0(17 市场 / 217 行),
   这条判决目前由体育盘扛着。**

---

## 与 PM 并排的头条数字

| | Kalshi(我们) | Polymarket(Dubach) |
|---|---|---|
| 中间档(40-60¢)报价价差 | 1,414–2,316 bps(全交易所)/ **230 bps**(crypto 15M) | 400 bps |
| 最低价档(<10¢)报价价差 | 6,667 bps(= **3¢**) | 1,818 bps(= 0.9¢) |
| L1 份额中位 | 0.1472 | 0.1364 |
| L2 份额中位 | **0.1928(> L1)** | 0.1034(< L1) |
| KL(份额‖均匀)中位 | 0.150 | 0.087 |
| 强 top-heavy(L1>0.5) | **1.4%** | 9% |
| L1 份额落在 [0.05,0.20] | 65.7% | 57% |
| SF8 全规格 log(tte) 系数 | **+0.251**,CI 排除 0(市场内) | +0.008(横截面) |
| SF8 全规格 log p(1−p) 系数 | **+0.132**(靠近 50¢ 书更厚) | **−1.02**(价格越极端书越厚) |
| 交易方向来源 | `taker_side`,交易所真值 | 盘口推断,与链上真值仅 **59%** 一致 |

**PM 全文最大的痛点在我们这里不存在。** 他从 WebSocket 推断的方向只有 ~59% 对,
导致 top-100 面板里 **67% 的市场有效价差变号、60% 的 Kyle's λ 变号**。
我们的 `taker_side` 是交易所回执字段,已自连接验证。所以 SF5-K 这一页在他的方法学下根本无法可信地产出。

---

## 已知答案门(这次最重要的一段工程)

L2 是增量流,必须重建;重建**必须**先证明它重建对了(反例 C1:grid_replay v1 无门,
打出 199,482 笔假成交、整轮作废)。

- 门的做法:每个市场从自己的第一条 snapshot 起重放,重放出的最优买/卖价拿去
  **逐点比对该市场自己的 L1 报价时间线**——两条 L2 事件之间持有的书状态,必须复现落在该区间内的每一条 L1 报价。
- 结果:**1,975,327 次比对,847 个市场,中位一致率 1.0000,752 个市场(89%)≥99%**。
  放宽到 ≥95% 是 770 个,形状统计量分毫不差(L1 份额 0.1472 vs 0.1478,KL 0.150 vs 0.151)
  ——门的选择没有在挑形状。
- 门还**证伪了一个假设**:重放顺序不能想当然。抓取会周期性重订阅,`ws_seq` 是**每订阅**计数的,
  所以同一个体育市场按 `ws_seq` 排会把两段不同时间交错在一起;而 crypto 15M 活在单一订阅里,
  `ws_seq` 才是真序、`ts_utc` 反而是错的。实测(九个市场对 L1 真值):
  crypto seq 0.94-0.98 / ts 0.02-0.31;体育 seq 0.02-0.15 / ts 0.92-0.96。
  因此**两种顺序都重建一遍,由门逐市场裁决**(最终 ts 705 个 / seq 47 个)。
  去重(丢掉相同 `(ts, side, price, delta)` 的增量)让两边都变差 ⟹ 那些重复是真实的簿记事件。

**第一版的门本身是错的**:它拿 60 秒网格上的重建状态去比对一条可能陈旧 1 秒的 L1 报价,
在快速的 crypto 书上这是构造性的不一致(当时中位一致率只有 0.79)。
**门写错了会把对的重建判死**——和门缺失一样危险。

---

## 覆盖与 join 账

| | 数量 |
|---|---|
| L1 聚合行(市场 × tte 段) | 732,637 |
| SF1-K 入表市场 / 两侧齐全报价秒 | 35,979 / 444.81M |
| SF5-K 入表市场 / 成交笔数 / 张数 | 18,773 / 28.83M / 3.21B |
| SF2-K 入表市场 / 书采样点 | 210 / 84,873 |
| SF8-K 面板行 / 市场 | 28,118 / 226 |
| 结算索引已终结二元市场 / 快照截止内 | 2,899,760 / 2,360,096 |

**窗口右端是 07-24 不是 07-25/26**:唯一 DONE 的 catalog 快照拍摄于 2026-07-24T21:30:17Z,
之后收盘的市场没有结算真值,拿不到 tte。

**L2 是配额抽样不是随机样本**:干净三日只覆盖 Crypto/BTC 与 Sports 六个 subcategory,
crypto 每小时只抓约 1 个 15M 市场(07-15 共 24 个)。SF2-K 的 crypto 15M 只有 16 个市场进入十档表,
**只作结构描述,不承载统计推断**。

---

## 反例 15 四件套(随报告存档)

1. **分类规则版本**:`taxonomy.py` sha256 前 16 位 `91dd1d4cdd7fe40b`
2. **catalog 快照哈希**:`snapshot=20260724T213017Z`
3. **逐 ticker 归属表**:`ticker_family_map.parquet`(173,905 个 market_ticker,其中 95,305 个在六族内)
   + `census.parquet`(2,453 条 series × 族 × 行数)
4. **未识别清单**:71 条 series / 38.94M L1 行。按行数排头部全是
   **KXSOLD / KXSOLE / KXBNB / KXBNBD / KXXRPD / KXHYPED / KXXRP / KXHYPE**
   —— `taxonomy.py` 的 `CRYPTO_HOURLY` 只写了 `{KXBTCD, KXETHD, KXBTC, KXETH}`,
   **SOL/BNB/XRP/HYPE/DOGE 整族从未被分类过,因此从未被任何一份分族报告看见过**。
   被整类排除的类目:Exotics 509.04M 行、Commodities 37.46M、Financials 31.84M(均不在六族定义内)。

> **这一条是本次实验的一个真实待办**,不是发现:要么把这些 series 纳入 `crypto_hourly`,
> 要么显式声明只做 BTC/ETH。在改之前,所有「crypto 小时盘」结论的覆盖范围必须按此注明。

---

## 意外清单

十条,全文见 PDF 第 11-12 页与 `surprises.json`。分类:
A = 与 PM 锚点方向相反;B = 与我们已有结论冲突;C = 族间差异超预期一个量级;D = 形状不对劲。

值得立独立实验的六条:①第二档比第一档厚(挂单位置 × 事后 markout);
③书的两侧总量不对称(与 E11 方向毒性并排看是否同号);
⑤临近结算深度撤退(补 L2 覆盖后在 crypto 上复验);
⑥sports_prop 高价档 5.79¢ 有效半价差 × maker 费的净边际;
⑦crypto 15M 墓地区「书在/书不在」重新分层(E11 的墓地结论建立在有成交的样本上);
⑧行权价阶梯深度峰值在 $300-600 外(**必须先分离价位与距离**,当前两者完全混杂)。

---

## 重跑

```bash
# 1) 重活(EC2 生产盘,只读封存数据,输出只落 scratch)
rsync -az tools/research/replication/ ubuntu@EC2:/home/ubuntu/e15_scratch/replication/
ssh EC2 'cd /home/ubuntu/e15_scratch && nice -n 19 ionice -c3 \
    /home/ubuntu/hft-bot/.venv/bin/python replication/experiments/microstructure_e15.py all'
# 2) 取回工件(约 40 MB)
rsync -az ubuntu@EC2:/home/ubuntu/e15_scratch/out/ e15out/ --exclude settlement.parquet
# 3) 统计(Mac,无重 IO)
python3 tools/research/replication/experiments/microstructure_e15_analyze.py e15out results.json
# 4) 出 PDF(REPORT_PNG 逐页看图再发)
REPORT_PNG=png python3 tools/research/replication/experiments/microstructure_e15_report.py \
    results.json MICROSTRUCTURE_E15_2026-07-27.pdf meta.json
```

运行时间:settle+census ~2 min,l1 ~3 min,trades ~4 min,l2+gate ~9 min,vol+ladder ~1 min。
**重的逐市场工件(`l1_market_band/` 24 MB、`eff_spread/` 15 MB、`depth_panel.parquet`)留在
EC2 `/home/ubuntu/e15_scratch/out/`,没有进仓库**;本目录里的 `results.json` 足以重出整份 PDF。

## 代码

- `tools/research/replication/experiments/microstructure_e15.py` — 八个 pass(settle/census/l1/trades/l2/gate/vol/ladder)
- `…_analyze.py` — 统计层(聚类 bootstrap、KL、市场内面板 + 聚类三明治 SE、阶梯隐含现货插值)
- `…_report.py` — PDF 渲染
- `tools/research/replication/report.py` — 本次为它加了 **CJK 模式**(`Report(..., cjk=True)`)
  与**按显示宽度换行**:matplotlib 不会逐字回落字体族(实测 3.9.4),DejaVu 没有汉字,
  中文报告不开这个模式会整页变成方框。

## 这份报告不产生什么

不产生交易规则;不改任何引擎参数;不碰实盘。样本内形状进入引擎的唯一通道是独立的样本外实验
(反例 13)。「平均在场者」的读数不是我们的 EV(反例 14)。
所有 CI 是市场聚类 bootstrap×1000,不是按笔数算的 n(反例 3)。
