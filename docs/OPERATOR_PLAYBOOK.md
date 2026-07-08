# OPERATOR PLAYBOOK — session-by-session paste queue

**How to use:** open a FRESH agent session, paste the topmost unchecked
prompt VERBATIM, let it finish (including exit ritual + independent audit),
check the box, commit the checkbox edit. One prompt per session — never
combine entries.

**Authority:** ordering comes from docs/MASTER_SEQUENCE.md (operator-authored,
supersedes this file if they ever disagree). This file is a convenience layer:
the same queue rendered as ready-to-paste prompts. Keep it updated as part of
each session's exit ritual (E5).

Snapshot date: 2026-07-07. Queue state: gold contract done; capture hardening
W-C0/C1/C2 done+audited; STEP 0 deployed; W-D0 dashboard requirements
approved+audited; B1 ruled (collectors wait for EC2).

---

## Queue (top = next)

- [x] **1. Supervisor daily wiring — W-C2.1 + coverage_audit (combined: same
  file, same risk class, one P4 review).** DONE 2026-07-08 (commits 9878025 +
  7ced1a7; independent audit: no blocking defects; not active until the next
  supervisor restart). Operator approval to touch
  pipeline_supervisor.sh is GRANTED by pasting this (satisfies the
  operator-gate in next_actions.md item 1 and W-C2's audit note). Paste:

  > 读 docs/GUARDRAILS.md 和 docs/SESSION_LOG.md 最新条目后，执行合并的
  > supervisor 每日接线（操作员已批准动 pipeline_supervisor.sh）：
  > (a) W-C2.1：把 `capture_gaps --date <昨天>` 接进每日导出块，并接上
  > `--live` 告警（写 work/live/capture_alert.json 的那个）；
  > (b) next_actions.md 第 1 项：同一每日块里跑
  > `python3 tools/coverage_audit.py --date <yesterday>`，非零退出在
  > supervisor 日志里可见。
  > 要求：说明采集连续性如何保证（P4）；新行为配测试（E1）；`make check`
  > + `tests/run_pipeline.sh` 全绿；更新 next_actions.md 勾掉已完成项 +
  > 本文件勾掉第 1 条（E5）；退出仪式 + 独立审计。
  > 背景：SESSION_LOG「2026-07-08 00:55 UTC」条目 PROCESS NOTE。

- [ ] **2a. OPERATOR ACTION — supervisor restart, after 2026-07-09 00:00 UTC
  only** (ruled 2026-07-08): item 1's wiring is dormant until the running
  supervisor (PID 4314/4323 era) restarts. Do NOT restart before 07-09
  00:00 UTC — it would stamp a fresh gap onto 2026-07-08, the W-C3
  measurement day. After midnight UTC: restart, then verify (i)
  work/live/capture_alert.json appears within 60s, (ii) the restart's own
  small gap shows up in 07-09's record — the detector logging its own
  restart is the wiring working. Gap records manually backfilled through
  07-08, so the 3-day retention clock is safe meanwhile.

- [ ] **2. W-C3 acceptance tail — TIME-GATED: run only after 2026-07-09
  00:00 UTC** (needs the full UTC day 2026-07-08 on disk; do 2a first,
  order irrelevant to the 07-08 measurement). Paste:

  > 读 docs/GUARDRAILS.md 后执行 W-C3 验收尾巴：跑
  > `capture_gaps --date 2026-07-08`，预期整天只有 00:00:00→00:10:25 那个
  > 部署前的洞（W-C1 部署时留下的，已知）。结果无论好坏写进
  > SESSION_LOG + 勾掉本文件第 2 条；若发现新缺口，按 W-C2 审计的口径
  > 红字先行修复。退出仪式。

- [x] **3. W-D1 dashboard 设计稿 — APPROVED 2026-07-08 (addendum ⑥⑦, design frozen) — anytime, no dependencies, STOP at the
  end (you must approve the design before any dashboard code).** Paste:

  > 读 docs/GUARDRAILS.md 和 docs/PLAN_DASHBOARD_OBSERVATORY.md 后，执行
  > W-D1：做全部 7 个 tab 的静态 HTML 设计稿（无真实数据），每个组件标注
  > 对应 W-D2..D5 的数据字段；必须体现：green / UNKNOWN / MODULE NOT LIVE
  > 三态视觉区分、超龄工件的 UNKNOWN 渲染、TREND RULE（live statistic 带
  > sparkline，state 豁免）。产出放 sandbox/，设计说明写
  > docs/plan_audits/dashboard_design_wD1.md。这是 STOP：操作员批准设计
  > 之前不写任何生产代码。退出仪式。

- [ ] **3b. W-C5 — hour-boundary capture holes (CRITICAL PATH to the
  seven-clean-days gate: while unfixed, every day gets stamped ~3min of
  gaps and clean days never accumulate).** Production capture code — full
  W discipline, NOT sandbox. Paste:

  > 读 docs/GUARDRAILS.md、docs/BACKLOG.md 的 W-C5 条目和 SESSION_LOG 后，
  > 执行 W-C5：诊断并修复 ws_shadow 整点边界的非零退出/重试循环缺口
  > （07-08 02:00 附近 ~3 分钟洞是首个已钉案例，见 incident forensics
  > CASE #1）。要求：先诊断写明根因，再红字先行修复；P4 说明采集连续性；
  > `make check` + `tests/run_pipeline.sh` 全绿；结果对照 capture_gaps
  > 真实数据验证（修后整点无新洞）；退出仪式 + 独立审计。

- [ ] **3c. W-R — PDF report pipeline (operator standing requirement,
  2026-07-08): every daily/acceptance-class detection emits a typed PDF
  report with numbers into a fixed Mac folder; post-migration EC2 reports
  flow back via the W-A5 S3 sync.** Paste:

  > 执行 W-R：建统一报告渲染器 tools/daily_report.py——读各检测的既有
  > 结构化产物（capture_gaps.csv、coverage/loader report、
  > lifecycle_status.json、incident/taxonomy 文档），按检测类型渲染成
  > 带数字的 PDF，落 **操作员指定文件夹（2026-07-08 指定）：
  > `/Users/ritcardo/Desktop/TradingSys Report/<YYYY-MM-DD>/<检测名>_<时间戳>.pdf`**
  > （文件名用人话检测名；报告目录在仓库之外，天然不进版本库——PDF 是
  > 派生副本，结构化文件才是权威，D1。目标文件夹不可写时降级写
  > 仓库内 reports/ 并记 WARN，不许因此阻塞管线，P4。
  > 迁移后 EC2 生成同结构目录，经 W-A5 的 S3 sync 每日落回 Mac 同一文件夹）。
  > 接进 supervisor 每日块（操作员已批准，同 item 1 先例）：每日体检与
  > 验收类检测跑完即出 PDF；60 秒级 watchdog 明确排除（走告警通道）。
  > 渲染失败只记日志不阻塞管线（P4）。字体用系统 CJK（中英混排必须
  > 都渲染，参照 2026-07-08 阅读清单 PDF 的踩坑）。红字测试：给定
  > 固定 fixture 产物 ⇒ 确定性 PDF（文本层可断言）；渲染器崩溃 ⇒
  > 管线无感。E5：RUNBOOK 加 reports 一节。退出仪式 + 独立审计。
  > **报告格式契约（操作员 2026-07-08，验收硬条款）**：每份 PDF 首页
  > 开头必须依次是——① 标题（检测名 + 日期，人话不用代号：
  > "采集缺口日报 · 2026-07-10"而不是"W-C2.1 output"）；② 一行裁决，
  > 三态：✅ 一切正常 / ⚠️ 有异常但不用你动手 / ❌ 需要你行动（+一句
  > 做什么）；③ 3-5 行白话简介：这个检测在查什么、这次结果是什么、
  > 关键数字意味着什么。数字表格与图一律放简介之后。验收标准：
  > 操作员只读前五行即知今天有没有事；裁决三态与 D2 对齐——
  > 数据缺失/检测没跑成渲染为 ⚠/❌，永不默认 ✅。

- [ ] **3d. Readability audit — READ-ONLY (operator complaint 2026-07-08:
  tool/variable naming not intuitive). Produces the refactor backlog for
  POST-migration execution; no code changes before cutover (frozen-code
  rule).** Paste:

  > 只读可读性审计：通读 tools/、src/、include/、apps/，产出
  > docs/plan_audits/readability_audit_<date>.md：① 命名欠账清单——所有
  > 误导性/不直观的工具名、函数名、变量名，每条给"现名→建议名→为什么
  > 现名会误导"；② 按模块的晦涩度排序（最难懂的文件排前）；③ 注释与
  > 代码脱节处；④ 每条标注修复成本（改名=小，重组=中，重设计=大）。
  > 全程零代码改动（迁移前代码冻结）；清单成为迁移后逐模块重构 W 的
  > 队列。退出仪式。

- [ ] **3e. 升舱 W — all categories to full L1 NOW on Mac (operator-approved
  2026-07-08; every day of delay permanently discards Class-B orderbooks
  beyond the 3-day raw window).** Paste:

  > 读 docs/GUARDRAILS.md 和 docs/DATA_COMPLETENESS_ROADMAP.md 拼图① 后，
  > 执行升舱 W（操作员已批准 2026-07-08）：① config/market_classes.yaml
  > 全部类别提升为 class_a_full_l1（Exotics 照存，仅保持 Q7 的 MM 候选
  > 排除——研究过滤≠存储过滤）；② 改动附带入库测试（D4：策略变更配测试
  > 同 change）；③ 立即用现存 raw（3 天窗口）把原 Class-B 类别的 L1 回灌
  > 入库和归档，provenance 不变；④ 写明磁盘增量预估 + 采集连续性如何
  > 保证（P4）；⑤ `make check` + `tests/run_pipeline.sh` 全绿；同步更新
  > warehouse_schema.md 两类策略章节 + TECH_STACK.md（E5）。
  > 验收 = 拼图① 完成判据：全类别当日归档 L1 行数>0 且 3 天回灌入账
  > （loader 报告佐证）。退出仪式 + 独立审计。

- [ ] **3f. PLAN_HISTORICAL_BACKFILL.md draft — paper only (P9).** Execution
  gated behind W-A4 (single REST owner). Paste:

  > 读 docs/GUARDRAILS.md、docs/MASTER_SEQUENCE.md STEP 5 和
  > docs/DATA_COMPLETENESS_ROADMAP.md 拼图② 后，起草
  > docs/PLAN_HISTORICAL_BACKFILL.md（纯文档，不执行任何爬取）：
  > 七字段 W 定义——settlements 全量优先（解锁校准研究），然后开机前
  > trades/candles 历史；endpoints 逐一对照 docs/vendor 的 Kalshi spec
  > 验证（禁止凭记忆写 API）；read-token 预算上限；
  > provenance=rest_backfill 列，永不与 ws_capture 混桶；断点续爬；
  > 首次真实爬取 operator-gated；含 §6 自审计。
  > 验收 = 拼图② 完成判据照抄进计划的 Acceptance。退出仪式 + 独立审计
  > （P9：可与其他纸面活同会话，审计合并一次）。

- [ ] **3g. 深度名单扩容 W — Sports-first tick-level L2 on Mac NOW (operator
  strategic focus 2026-07-08: 体育市场为主攻方向).** Paste:

  > 执行深度名单扩容 W（操作员批准，STEP 4 的 Mac 先行版，体育优先）：
  > ① 建 config/depth_watchlist.txt（补上 next_actions.md 第 2 项的声明
  > 清单缺口），按最近 3 天成交量选 top N 市场——**Sports 占主体** +
  > Crypto 刻钟盘，N 初始 50，写明按量调整规则；② ws_shadow 按名单订阅
  > orderbook_delta（快照+增量+ws_seq），沿用现有 orderbooks_full 通路；
  > ③ D4 入库测试同 change；④ 渐进上量：先 10 个市场跑 1 小时打印
  > 带宽/磁盘实测，再扩到 50（P4 采集连续性声明）；⑤ `make check` +
  > `run_pipeline.sh` 全绿；更新 warehouse_schema.md + roadmap 拼图③
  > 加"Mac 先行名单"小节（E5）。验收：名单市场整本簿逐笔入库、
  > ws_seq 连续、V15 覆盖审计从 declared_list_missing 转绿。
  > 退出仪式 + 独立审计。

- [x] **4. STEP 1 plan draft (SUPERSEDED — see 4-DONE note below) — PLAN_AWS_MIGRATION.md (paper only).**
  Prerequisite YOU must do first: have an AWS account ready (agent never
  touches account creation or credentials — S4). Paste:

  > 读 docs/GUARDRAILS.md 和 docs/MASTER_SEQUENCE.md STEP 1 后，起草
  > docs/PLAN_AWS_MIGRATION.md：七字段 W 定义（W-A0 sizing gate → W-A1
  > hardening → W-A2 Linux validation → W-A3 S3 vault + RESTORE TEST →
  > W-A4 zero-gap cutover（操作员 go/no-go）→ W-A5 post-cutover）+ STEP 1
  > 的三个 riders + §6 自审计。纯文档，不执行。退出仪式 + 独立审计。

- [x] **4-DONE note:** PLAN_AWS_MIGRATION.md is DRAFTED + independently
  audited (8 findings applied, 2026-07-08). Item 4's prompt is obsolete.

- [ ] **5–10. W-A0 → W-A5, one per session.** Generic paste (replace N):

  > 读 docs/GUARDRAILS.md 和 docs/PLAN_AWS_MIGRATION.md 后，执行其中的
  > **W-A<N>**，严格按该 W 的七字段定义（Allowed/Forbidden writes、
  > Acceptance 原文为准）。凭证与 env.sh 由操作员手工处理（S4），需要
  > 操作员动手/花钱/go-no-go 的节点停下来等确认。退出仪式 + 独立审计。

  W-A0 附加一句：「sizing 选定 ≥32GB 分支（gold 上 EC2，Mac 彻底退役）
  并记录进计划」。W-A4 你必须在场 go/no-go。

  **W-A0 纸面完成 2026-07-08（独立审计 PASS，0 阻断）**：选型 r8g.2xlarge
  （8 vCPU/64GB，us-east-2）+ 300GB gp3，~$380/月；决策、账目、开机清单全在
  PLAN_AWS_MIGRATION.md「W-A0 RESULT」一节。「退役」的准确含义也写在那里
  （Mac 退出生产管线，但保留日报落地任务 + 回滚能力，不可抹盘）。下一步 =
  操作员照清单开机（花钱节点，等批），W-A1 会话开头跑 3 条只读命令核对后
  关闭 W-A0。

- [ ] **11–14. W-D2 → W-D3 → W-D4 → W-D5 (dashboard collectors, ON EC2 —
  B1 ruling 2026-07-07),** prompts per their seven-field definitions in
  PLAN_DASHBOARD_OBSERVATORY.md. Unblocked by item 10 (cutover).

- [ ] **15. W-D6 dashboard implementation** — unblocked by items 3 (approved
  design) + 14. Then **16. W-D7 end-to-end acceptance + whole-plan audit.**

- [ ] **After that:** MASTER_SEQUENCE STEP 4 (depth expansion), event
  packaging insert, STEP 5 (historical backfill), STEP 6 (pricing +
  kill-switch plan drafts). Prompts to be added here when their turn nears.

---

## Things only YOU can do (agents are barred)

- AWS account + EC2 provisioning + `~/.kalshi/env.sh` on the box (S4).
- W-A4 cutover go/no-go; being present during it.
- Approving the W-D1 design (STOP) and any GUARDRAILS change.
- Fees OQ-1 ratification (standing gate, see MASTER_SEQUENCE).
- Any future live-trading confirmation (S1 — per-session, explicit, always you).

## Standing gates (nobody can rush these)

- Seven clean days of capture (counts only with positive coverage evidence —
  PLAN_DASHBOARD_OBSERVATORY §W-D4 / audit A3); earliest 2026-07-13.
- Live orders: ALL lifecycle gates + risk caps + tested kill switch + S1.

---

## 迁移完成 · 操作员亲自验收单（2026-07-08 定，切换后逐项打勾）

问 agent 要证据，每项要看到实物数字/文件，口头"没问题"不算：

- [ ] **数据零丢失**：双机重叠 diff 报告——重叠窗口内 EC2 漏检数字；
      Mac→S3 残余窗口回灌完成 + md5 校验输出。
- [ ] **金库可恢复**：W-A3 恢复演练的实际输出（从 S3 拉回并校验成功的记录），
      不是"配置了备份"。
- [ ] **金库删不掉**：EC2 所用 IAM 策略原文——确认无 DeleteObject；
      S3 版本化开启截图/输出。
- [ ] **EC2 采集健康**：切换后连续 24h 的 capture_gaps 报告 = 零缺口；
      freshness/msg rate 正常；ws_seq 连续。
- [ ] **测试全绿在云上**：EC2 上 `make check` + `run_pipeline.sh` 的输出尾巴。
- [ ] **Mac 正确退役**：launchd 已卸载但保留（随时可回滚）；
      恢复睡眠（W-A5 步骤，≥32GB 分支才允许）。
- [ ] **报告回流**：桌面 TradingSys Report 文件夹收到第一份 EC2 生成的
      每日 PDF。
- [ ] **成本白纸黑字**：首月预算表（EC2+EBS+S3+流出流量）+ 设定的上限。
- [ ] **随手取数演示**：现场从 S3 拉任意一天任意类别的数据（或 DuckDB
      直查 S3）跑通一次给你看。

九项全勾 = 迁移正式完结；缺一项 = W-A5 不算完成，不进入下一阶段。
