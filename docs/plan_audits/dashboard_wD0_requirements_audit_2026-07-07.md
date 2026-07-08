# Independent audit of PLAN_DASHBOARD_OBSERVATORY.md (W-D0 requirements), 2026-07-07

Source: operator-relayed independent audit, received in-session 2026-07-07.
Preserved VERBATIM below per the full-text preservation rule. Verdict and
remediation record follow at the end.

---

## Audit text (verbatim)

A1. W-D6 的沙箱和 Data/Overview 页的数据源自相矛盾 —— 按现在的规则 Data 页做不出来。 §2 表格规定 Data 页读 `metrics.ndjson`、`capture_gaps.csv`、`capture_alert.json`；Overview 页要 "gap count trend from capture_gaps.csv"。但 W-D6（前端）的 Allowed reads 只有 `work/observatory/` + 既有端点；而没有任何一个 collector 被指派把 gaps/alert 镜像进 observatory——W-D2 的 Allowed reads 里没有 capture_gaps.csv/capture_alert.json（只有 metrics、raw 时间戳、latency probe）；W-D3 读 alert 但产出的是 incidents，不是 gap 浏览器需要的原始序列。结果：W-D6 要么违反自己的 Allowed reads 直接去读那两个文件，要么 Data 页缺功能验收失败。修法二选一：把 gaps/alert 加进 W-D2 的采集范围（输出到 observatory），或明确扩大 W-D6 的 Allowed reads——但要写进文档，不能到时候临场变通。

A2. W-D7 验收要求"notification file within one refresh cycle"，但全文没有任何 W 创建通知机制。 W-D3 只写 `incidents.ndjson`。"notification file" 是谁的产物、什么格式、写到哪，未定义。按 GUARDRAILS 的精神这就是验收依据引用了不存在的工件。要么在 W-D3 的 Allowed writes 里加上通知文件并给出契约，要么把 W-D7 的这条验收删掉。顺带："one refresh cycle" 全文没定义刷新节奏（SSE 推送？轮询几秒？），这条验收目前不可客观判定。

A3. "7 个干净日倒计时"的计算方式有经典的 fail-open 漏洞。 W-D4 用 `capture_gaps.csv` 算 clean-day。gaps 文件只记录发现了的缺口——采集器整天没跑、或者 gap 检测本身坏了的那天,没有 gap 记录,会被算成"干净日"。这和这份计划自己最在意的 D2 原则（"不能读源的 collector 报 UNKNOWN,绝不 green"）直接冲突。clean-day 必须要求正向的覆盖证据（当天 metrics/freshness 序列存在且连续）,而不是"没有坏消息"。这一条建议直接写进 W-D4 的 Acceptance："无覆盖证据的一天 ⇒ 非 clean,计数器归零或 UNKNOWN"。

A4. observatory 工件自身的新鲜度没有契约 —— collector 死掉会导致 stale-green。 W-D7 只测了"删掉源文件 ⇒ 组件变 UNKNOWN"。但更现实的故障是：collector 挂了,readiness.json / catalog.json 还是三天前的旧文件,前端照读照绿。需要：(1) 每个 observatory 工件带 `generated_at`;(2) 前端对超龄工件渲染 UNKNOWN;(3) W-D7 加一条"停掉 collector ⇒ 相关组件在 N 秒内退化"的验收。另外 W-D4/W-D5 的 Allowed writes 里没有 supervisor wiring——那 readiness_snapshot.py 和 warehouse_catalog.py 由谁、以什么频率运行？没有运行节奏,Overview 横幅读的就是一张随缘更新的快照。

一致性/歧义（不修会在验收时扯皮）

B1. 文档开头和 W-D2 关于 STEP 1 排序自相矛盾。 头部说"Implementation Ws still queue behind STEP 1 (AWS)"；W-D2（明显是 implementation W:写代码、接 supervisor）却说 "Blocked by: nothing (amendment 2026-07-07); runs on Mac now"。要么存在第二份修正案没被引用,要么其中一句错了。这种主计划排序的含糊正是这套治理体系最不该容忍的。

B2. TREND RULE 缺少"live statistic"的定义,W-D6 的"zero bare numbers"审计不可执行。 kill-switch 状态、git sha、catalog 里每个数据集的行数、日期范围——这些天然是标量/属性,给行数画 sparkline 没有意义。需要一行定义:"随时间变化且用于健康判断的度量 = live statistic（必须带趋势）;状态、标识、目录属性 = state（豁免）"。否则要么审计员机械执法搞出荒谬的 sparkline,要么自由裁量让规则失去牙齿。

B3. MODULE NOT LIVE 和 UNKNOWN 对 go/no-go 横幅的关系没说清。 规则是"any UNKNOWN ⇒ banner 不能绿"。Backtest/Strategy/Execution 三个页在 W-D6 验收时全是 MODULE NOT LIVE 脚手架——如果它算 UNKNOWN,横幅在 STEP 6 之前永远不绿（警报疲劳,横幅作废）;如果不算,得白纸黑字写"MODULE NOT LIVE 是诚实的非部署态,不参与横幅",不然以后有人为了让横幅变绿悄悄把脚手架渲染成绿色,就是 D2 违规。

B4. §4 回测工件契约"现在钉死"但钉得不够死。 `ts` 单位没定（ns/ms/ISO? UTC?）;`fees_paid`、`qty` 的单位/定点比例没定（qty 是合约数还是 CountFp×100?）;整个契约没有 schema_version 字段——一个为未来引擎预先固定的契约,没版本号意味着第一次演进就要靠猜。这些是三行字的事,现在不写,STEP 6 的引擎和 W-D6 的渲染器必然对不上。

加固级（建议,不阻断）

* C1. W-D3 的 Blocked by 只写了 W-D2,但它实际依赖 W-C2 的 capture_alert.json 和被保存下来的 2026-07-07 401 原始片段。跨计划依赖和"验收数据已归档"应显式写进 Blocked by,否则开工才发现片段没留。
* C2. W-D4 要报"make check 上次何时在哪个 sha 通过"——前提是有东西记录测试运行。如果 lifecycle_check 不产这个记录,W-D4 的 Allowed writes 又不许碰 Makefile/检查脚本,这条功能没有原料。先确认记录存在,否则加一个前置小任务。
* C3. W-D3 解析 `ws_shadow.log`（非结构化日志）判 forced-reconnect 尖峰——脆弱。W-C1 已经加了监控计数器,让它落进 metrics.ndjson 走结构化路径更稳。
* C4. W-D5 对大 NDJSON 全量数行的 IO 成本、以及和采集进程的磁盘争用没有预算;latency.ndjson 512MB×3 的轮转和 Data 页"full time-series"的展示窗口也要对齐（轮转丢掉的历史,长趋势图从哪来）。

值得肯定的（不是客套,是审计意见的一部分）
用真实故障片段做验收（W-D3/W-D7 的 401 重放）、kill -9 非干扰测试、每个 W 的 forbidden writes 把爆炸半径钉死、"panic 只展示命令行不给按钮"、强制独立审计写进 W-D7——这些让这份计划比绝大多数同类文档可信。上面的 A1–A4 都是"计划自己的规则应用到自己身上"发现的洞,修掉之后这套流程是自洽的。

建议的处理顺序：A1、A2 是文字修订,五分钟改掉;A3、A4 要在 W-D2/D4 的 Acceptance 里各加一条;B1 找操作员裁决哪句是真;B2–B4 各补一段定义。全部改完再让 W-D1 进入设计,因为 UNKNOWN 渲染规范（W-D1 的核心产物）依赖 B2/B3 的裁决。

---

## Verdict (verified against doc + code, 2026-07-07)

| Finding | Verdict | Evidence / remediation |
|---|---|---|
| A1 | CONFIRMED | W-D6 Allowed reads expanded to name the pipeline-owned read-only files explicitly (no mirroring layer — a copy adds staleness for zero safety). |
| A2 | CONFIRMED | notify.json contract added to W-D3 Allowed writes; refresh cadence defined in W-D6 (5s poll); W-D7 criterion now objective (≤2× poll). |
| A3 | CONFIRMED | Classic fail-open, same class as W-C2 audit's B2/B3. Positive-coverage-evidence requirement written into W-D4 Acceptance. |
| A4 | CONFIRMED | Artifact envelope rule (schema_version, generated_at_us, source_sha, max_age) added as binding rule; W-D4/D5 gained supervisor wiring + cadence; W-D7 gained stop-collector degradation test. |
| B1 | CONFIRMED — OPEN | Real contradiction. Operator ruling pending; both sentences flagged in doc. Conservative reading (queue behind STEP 1) holds until ruled. |
| B2 | CONFIRMED | live-statistic vs state definition added to TREND RULE. |
| B3 | CONFIRMED | MODULE NOT LIVE defined as banner-exempt honest state, visually distinct from UNKNOWN; silent green-washing of a scaffold named a D2 violation. |
| B4 | CONFIRMED | schema_version, ts=int64 µs UTC, E4 money units, qty=whole contracts pinned in §4. |
| C1 | CONFIRMED, urgency UPGRADED | The "real 401 segment" existed only in the live log (~886KB, 488 401-lines) subject to rotation; no archived fixture. Preserved THIS SESSION to tests/fixtures/incidents/ (ws_shadow_401_lockout_2026-07-07.log + quality_log slice). W-D3 Blocked-by updated. |
| C2 | RESOLVED-GOOD | Record already exists: tools/lifecycle_check.py writes work/lifecycle_status.json + lifecycle_events.ndjson incl. core_tests stage. sha-if-absent ⇒ UNKNOWN noted. |
| C3 | CONFIRMED | forced_reconnects_ counter exists (ws_client.hpp:100,142) but is NOT exported to metrics.ndjson (verified). Log-parse stays as pinned-fixture fallback; metrics export proposed as capture-side rider (D4 test same change), operator-gated. |
| C4 | CONFIRMED | Catalog mtime/size cache + nice cadence; latency_daily.ndjson downsample added so long trends survive 512MB×3 rotation. |
