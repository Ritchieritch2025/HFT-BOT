# AUDIT PROTOCOL — cross-agent execution & audit rules (operator-approved 2026-07-10)

中文摘要:执行者与审计者必须是不同的 agent,优先跨厂牌(Claude 的活
Codex 审,Codex 的活 Claude 审);审计员必须亲自运行验收命令,禁止
只读代码;高风险 W 双审计;变异抽查验证测试真实性;SESSION_LOG 记录
谁执行、谁审计。以下为约束全文(agent-facing, English)。

## 1. Roles

- **Executor** — implements one W per session (Claude Code in VS Code is the
  default executor; Codex may execute too).
- **Auditor** — reviews the completed W. MUST be a different agent than the
  executor; **cross-vendor preferred** (Claude-executed W → Codex audits;
  Codex-executed W → fresh Claude session audits). Same-model audits are
  allowed only when cross-vendor is unavailable, and must be a FRESH session.
- **Meta-audit / planning layer** (web Claude / Cowork) — drafts plans,
  consolidates findings from multiple auditors, spot-checks audit quality,
  runs discipline sweeps (SESSION_LOG vs commits, doc drift, BACKLOG).

## 2. Audit rules (binding)

- A1 **Auditors RUN the acceptance.** Reading code and nodding is not an
  audit. The W's Acceptance commands are executed verbatim; outputs quoted
  in the audit report. "Demonstrated, not described" (P3) applies to audits.
- A2 **Auditors never fix.** Findings go to a table (severity HIGH/MED/LOW,
  evidence, suggested fix); the executor (or a new session) fixes; the
  auditor re-checks. One exception: typo-level doc fixes may be committed by
  the auditor if labeled as such.
- A3 **Checklist is GUARDRAILS §6** (10 questions) + the W's own seven-field
  definition. Every answer cited with file/line or command output.
- A4 **Mutation spot-check (for Ws that ship tests):** the auditor flips one
  economically meaningful sign/branch in the code under test (locally,
  never committed) and confirms the test suite goes RED. A suite that stays
  green fails the audit (tests are decorative). Q9 sign tests are priority
  targets.
- A5 **Independent fixtures (for strategy/data Ws):** the auditor constructs
  at least one fixture from the spec alone (not copied from the executor's
  fixtures); code must pass both.
- A6 **High-risk Ws get dual audit:** anything touching strategy math, risk
  caps, kill switch, or order paths is audited by BOTH a cross-vendor agent
  and a fresh same-vendor session; findings are unioned.
- A7 **Audit reports live in `docs/plan_audits/`**, named
  `audit_<W-or-doc>_<date>.md`, and end with a verdict:
  PASS / PASS-WITH-FINDINGS / REJECT.
- A8 **SESSION_LOG records executor and auditor** for every W:
  `executed-by: <agent/tool>` · `audited-by: <agent/tool>` in the entry.
  Self-audit of one's own session is void.

## 3. Plan review circuit (for plans, before execution)

Plans (PLAN_*/PROPOSAL_* docs) get multi-agent passes BEFORE the first
execution session. **Checklists catch compliance errors, not framing bias**
— so the circuit uses de-biasing REVIEW FORMS, not just re-reading:

1. **Round 1 — independent re-derivation (strongest de-biaser).** The
   reviewing agent does NOT read the plan first. It gets the goal + the raw
   inputs (research docs, GUARDRAILS) and derives its own minimal plan.
   THEN it reads the actual plan and diffs: divergences = framing blind
   spots of one side or the other. Feasibility checks (paths/tools/data
   verified on the real machine) ride along. Output to `docs/plan_audits/`.
2. **Round 2 — pre-mortem, cross-vendor.** Instruction is NOT "review this"
   but: "assume this system failed / lost the bankroll N months from now —
   write the post-mortem." Plus persona rotation: the courtsider exploiting
   us, the exchange compliance officer, the accountant explaining where the
   capital went. Forces generation of failure paths instead of validation.
3. **Round 3 — consolidation (web Claude).** Merge findings, bump plan
   version. **Conflict-of-interest control (the consolidator often authored
   the plan):** every finding quoted verbatim; rejected findings listed
   separately WITH reasons, shown to the operator; on request the rejection
   list goes back to the Round-2 agent for a final objection.
4. **Operator approval** of the revised version → execution begins.

Rounds 1-2 are read-only for the plan file itself; only Round 3 edits it.
**Time-box: one circuit + at most one recheck.** The strongest de-biasing
tool is empirical contact (the first data-producing W), not more review
rounds — reviews converge the plan, data corrects it.

## 3.5 Governance tiering — weight follows blast radius (operator-ratified 2026-07-10)

Audit depth is set by what the work can DESTROY, not by how recent the
document is. Ten lines, binding:

| Tier | Work touches | Required governance |
|---|---|---|
| T0 | Order paths / risk caps / money movement / kill switch | Full: dual audit (A6), mutation check (A4), independent fixtures (A5), pre-mortem review before design freeze |
| T1 | Pipeline / capture / non-regenerable data / config that changes production behavior | executor≠auditor + auditor RUNS acceptance (A1); single cross-vendor audit |
| T2 | Read-only research (scans, backtests, calibration) — worst case = a wrong number that real data exposes; rollback = delete file | Single audit round: auditor reruns the commands, checks sample sizes; done |
| T3 | Pure documents / plans | One adversarial reading + the operator's own eyes; the full review circuit (§3) is reserved for T0/T1 designs (first target: Phase-1.5 pricing/quoting logic) |

Independent re-derivation (§3 R1) is a silver bullet: spend it only where a
wrong FRAME bleeds money continuously (T0 designs), never on tasks with one
obvious minimal shape. Pre-mortem's real target is the live trading loop
("micro-live lost the bankroll in 3 weeks — write the post-mortem").

**Governance freeze:** until W-S1 produces data, NO new governance
documents or mechanisms are added to this repo. Data is the only auditor
that shares no priors with Claude or Codex — put it on the job.

## 4. Scope

Applies to all Ws and plans from 2026-07-10 onward. Does not retroactively
invalidate completed Ws. sandbox/ work stays exempt (P8). This file changes
only with operator approval (same rule as GUARDRAILS).
