# PLAN — Dashboard TUI v2(全系统监控终端)

状态:**设计稿 v0,2026-07-20,待操作员审批**(对应 w05 观测台规划的
W-D1 环节;本稿是它的升级版需求 + 布局设计)。
上游依据:`sandbox/w05-recovery-fix/docs/PLAN_DASHBOARD_OBSERVATORY.md`
(W-D0 已批规则全部继承:TREND RULE、诚实三色、数据信封、只读 S5、
高密度终端风)。

## 0. 操作员需求原文(2026-07-20,本窗口)

> 数据采集管道健康指标;策略研究进展进度条;最重要的市场观察窗口,
> 能甄别有机会的市场;whale order tracking;volume tracking with
> graphs;极简终端风(TUI),最高数据密度、闪电可读、键盘驱动。

## 1. 数据现实(每个数字有出处,2026-07-20 实测)

| 数据源 | 状态 | 证据 |
|---|---|---|
| `sandbox/expt_bo2026/` watchtower | **活**,3 秒轮询全市场成交 | `whale_alerts.ndjson` 49,564 行,mtime 今天 17:11;`watchtower_state.json` 每轮更新 |
| `work/metrics.ndjson`(引擎流水) | **死**,停在 07-09 19:09(EC2 割接日) | 文件 mtime |
| `work/warehouse/staging.duckdb` | **死**,最新 trade ts = 07-09 | duckdb 查询 max(ts_utc) |
| EC2(3.130.232.109)管道 | 生产真相在那边,Mac 只能靠拉取 | AWS 迁移记录 |
| `com.ritcardo.kalshi-report-pull`(Mac 拉取任务) | **在跑但报错**,退出码 127 | `launchctl list` |

结论:**Mac 本机唯一的活数据是 watchtower**。市场观察窗口第一版全部
建在它上面;管道健康指标必须从 EC2 拉,拉不到就显示 UNKNOWN(琥珀),
绝不留过期的绿。

watchtower 已有的内存状态(`watchtower/state.py`):每市场 10 分钟滚动
窗(带方向的美元流、合约数、价格)+ 1 小时基线窗 + VPIN + 库存/成交数。
已校准阈值(`config.py`,11 天带子 p99.99 校准):WHALE $500 / NOTABLE
$250 / BURST $2000@0.7 失衡 / SURGE 4× 量能 + 5¢ VWAP 位移。
**甄别机会的原料已经在跑,只差落盘和展示。**

已知缺口:watchtower 只轮询成交(trades),没有盘口(L1)——所以
spread/盘口深度在 Mac 侧第一版**没有**,标 MODULE NOT LIVE,等 EC2
firehose 接通后补。

## 2. 一屏布局(单页 TUI,等宽 11-12px,GitHub-dark)

```
┌─ STATUS ─────────────────────────────────────────────────────────────────────┐
│ EC2:capture ok 3s ▁▂▁▃ │ ingest lag 41s ▂▃▂▁ │ disk 62% │ WT alive 3.0s │ KS: — │ 17:12:04Z │
├─ MARKET WATCH ────────────────────────────────────────────── [1] ────────────┤
│  TICKER              CAT     ¢    Δ5m   $10m  net$   imb  vpin  whl  surge   │
│ ▸KXMLBGAME-…BALBOS   Sport   41   +3   2,140  +1.8k  .84  .41    3   5.2×▁▃▇ │
│  KXBTCD-…T65399      Crypto  27   -1     980   -320  .33  .77 ⚠  1   2.1×▂▂▃ │
│  …(前 30 行,按机会分排序,j/k 移动,Enter 进详情)                          │
├─ WHALE TAPE ──────────[2]──┬─ VOLUME BY CATEGORY ──────────[3]───────────────┤
│ 17:11 WHALE  yes $634 MLB… │ Sports  ▂▃▅▇▅  12.4k$/10m                       │
│ 17:11 NOTBL  yes $266 BTC… │ Crypto  ▁▂▂▃▂   3.1k$/10m                       │
│ 17:10 BURST  no  $2.1k …   │ Mention ▃▂▁▂▅   1.9k$/10m  (等高 small multiples)│
├─ RESEARCH ────────────[4]──┼─ PIPELINE(EC2)──────────────[5]───────────────┤
│ BO-1  shadow  ████████░ 8/9│ capture fresh  3s ▁▁▂▁   gaps today: 0          │
│ RC-4  shadow  ██████░░░ 6/9│ WS RTT p99  6ms ▂▁▂▃    seal 07-19 ✓            │
│ S3    pilot   ███░░░░░░ 3/9│ alert: (none)            disk 62% ▁▁▂           │
├─ TAPE(raw tail,space 暂停)────────────────────────────────────────────────┤
│ {"kind":"VPIN_RESUME","ticker":"KXWTACHALLENGERMATCH-…","vpin":0.8,…}        │
└──────────────────────────────────────────────────────────────────────────────┘
```

键盘:`1-5` 面板焦点 / `j k` 行移动 / `Enter` 市场详情(价格+量能图,
读 `expt.duckdb` 历史)/ `f` 按类别过滤 / `s` 轮换排序列 / `space`
暂停 tape / `?` 键位表。鼠标可用但一切操作有键位。

## 3. 各面板数据合同(来源 / 刷新 / 过期即 UNKNOWN)

| 面板 | 来源文件 | 刷新 | max_age |
|---|---|---|---|
| STATUS + PIPELINE | `work/ec2_mirror/health.json`(新,EC2 拉取) | 60s 拉一次 | 180s |
| MARKET WATCH | `sandbox/expt_bo2026/market_snapshot.json`(新,watchtower 落盘) | 每 3s 轮询写 | 30s |
| WHALE TAPE | `whale_alerts.ndjson` + `bo_vpin_alerts.ndjson` 追读 | SSE 实时 | 120s |
| VOLUME | market_snapshot 按类别聚合 | 3s | 30s |
| RESEARCH | `work/research_progress.json`(新,人工/会话维护) | 读文件 | 状态类,豁免 |
| 详情图 | `sandbox/expt_bo2026/expt.duckdb` 只读 | 按需查询 | 查询时校验 |

## 4. 机会分(v0,可解释、可调)

排序分 = 量能激增倍数(10 分钟 gross $ ÷ 自身小时基线,SURGE 逻辑)
为主键;行内附:净流方向、失衡度 |net/gross|、鲸单计数(1h)、VPIN。
**VPIN ≥ 0.8 的行标 ⚠(毒流,做市该退避,吃单方向另说);价格出
10-90 内区的机械收敛行降权(INTERIOR 规则,Mentions/Companies 豁免,
沿用 config.py 现行校准)。** 明示:这是启发式 v0,阈值都来自 11 天
带子校准,每周该重校;公式全部展示在 `?` 帮助页,无黑箱。

## 5. 要新造的三个小件(都不碰交易路径,全只读或旁路落盘)

1. **`market_snapshot.json` 落盘**:watchtower 已在内存里算好一切,
   加一个 sink 每轮把 screener 行写成 JSON(带数据信封)。改动点
   `watchtower/daemon.py`+`sinks.py`,几十行。
2. **`work/research_progress.json`**:机器可读的研究进度
   `{line, stage, done_steps, total_steps, note, updated}`;各研究会话
   退出仪式时更新。仪表盘只读渲染。
3. **EC2 健康拉取修复**:`kalshi-report-pull` 现在退出码 127(命令找
   不到),先修好;顺带让它把 `capture_alert.json` + 延迟快照拉到
   `work/ec2_mirror/`。拉不到 ⇒ 面板 UNKNOWN,这是特性不是缺陷。

## 6. 交付队列(每步可单独验收)

- **T-1 活数据设计原型**(sandbox/dashboard_v2/,一次性):真数据、
  零编造,按 §2 布局出全屏,操作员过目定稿。(= W-D1 精神,静态假
  数据 mock 已被 07-08 裁决否决,不重蹈。)
- **T-2** watchtower snapshot sink(§5.1)。
- **T-3** research_progress.json 规范 + 首版数据(§5.2)。
- **T-4** EC2 拉取修复(§5.3)。
- **T-5** 并入 `dashboard_server.py`:保留已审计后端(只读/本机/
  live_order 403),新前端 + 新只读端点,测试钉死。
- **之后**:EC2 侧 W-D2..D5 采集器按原观测台规划推进(已解锁——
  迁移完成)。

## 6b. 决策记录(烤问会 2026-07-21,操作员在场)

操作员总裁决:**"全部跟你的决定来走,我只看测试结果和利润"**——技术细节
授权工程侧,验收标准 = 测试结果 + 利润。以下按此授权裁定:

1. **机会分双视角,默认做市视角**(操作员选 A):`m` 键切换
   做市分 ↔ 跟鲸分,表头明示当前视角。
2. **做市分配方一切按论文**(操作员裁定,"一切都根据论文的理论来走"):
   依据 `docs/MM_DOCTRINE_FROM_LITERATURE.md`——
   做市分 = (base$/h + whale$1h) × 类别系数(单名/体育 1.0、中间类 0.6、
   宽基 0.1;B1 + 自家复现宽基 pre-fee≈0)× 中段系数(30-70¢=1、
   10-90=0.5、其余 0.15;B2/B5)× (0.5 + YES 流占比;B2/B8 乐观税)。
   **VPIN 不计分**——毒市场是主战场(B6),毒时点才暂避(B4 悬崖),
   行内"避"标记;phrase_risk 显示"条款"标记(B8 Netflix 定式)。
   公式属 v1 排序启发式,零交易风险,后续拿榜单 vs 事后结算 P&L 实证校验。
3. **跟鲸分激增封顶 20×**,显示原值;无基线且无鲸单的死盘激增项 ×0.2。
4. **架构**:正式版仪表盘保持只读文件;标题/盘口等网络抓取下沉到采集器
   (T-2);原型阶段允许 `--allow-network` 直连(公开只读 GET)。
5. **部署**:仪表盘留 Mac(本地活数据 + EC2 镜像 T-4),暂不搬 EC2。
6. **T-5 并入**:新面板成为 dashboard_server.py 新前端,Tests/Tools
   页签与既有端点保留(与 w05 W-D0 "留后端换前脸"裁决一致)。
7. **watchtower 重发 bug 在 T-2 修源头**(实测同笔成交 5-8s 内重发,
   USD 精确一致;仪表盘侧已用 30s 窗口去重兜底),重启 daemon 挑安静
   时段,前后对数验证。
8. **research_progress.json 由各研究会话退出仪式维护**。

## 7. 不做的事

无下单、无 panic 按钮(只显示 kill-switch 状态 + 可复制命令行)、
无远程暴露(localhost;看 EC2 走 SSH 隧道)、研究绘图引擎不进仪表盘
(报告归报告,Visualize-Everything 裁决管报告那边)。
