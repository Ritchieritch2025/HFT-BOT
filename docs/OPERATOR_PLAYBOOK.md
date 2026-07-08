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

- [ ] **4. STEP 1 plan draft — PLAN_AWS_MIGRATION.md (paper only).**
  Prerequisite YOU must do first: have an AWS account ready (agent never
  touches account creation or credentials — S4). Paste:

  > 读 docs/GUARDRAILS.md 和 docs/MASTER_SEQUENCE.md STEP 1 后，起草
  > docs/PLAN_AWS_MIGRATION.md：七字段 W 定义（W-A0 sizing gate → W-A1
  > hardening → W-A2 Linux validation → W-A3 S3 vault + RESTORE TEST →
  > W-A4 zero-gap cutover（操作员 go/no-go）→ W-A5 post-cutover）+ STEP 1
  > 的三个 riders + §6 自审计。纯文档，不执行。退出仪式 + 独立审计。

- [ ] **5–10. W-A0 → W-A5, one per session,** prompts live in
  PLAN_AWS_MIGRATION.md once item 4 is done. W-A4 cutover requires you
  present for go/no-go.

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
