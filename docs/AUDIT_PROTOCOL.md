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
execution session:

1. **Round 1 — Claude Code review:** feasibility on the real machine (paths,
   tools, data availability, runtimes), missing steps, seven-field
   completeness. Output: findings table appended to `docs/plan_audits/`.
2. **Round 2 — Codex review:** adversarial pass — internal contradictions,
   GUARDRAILS conflicts, silent-failure surfaces, acceptance loopholes
   (anything that could "pass" while wrong — D2 in plan form).
3. **Round 3 — consolidation (web Claude):** merge findings, revise the plan
   (version bump), list rejected findings with reasons.
4. **Operator approval** of the revised version → execution begins.

Rounds 1-2 are read-only for the plan file itself (findings go to
plan_audits); only Round 3 edits the plan. A plan that skipped the circuit
may not enter execution unless the operator explicitly waives it.

## 4. Scope

Applies to all Ws and plans from 2026-07-10 onward. Does not retroactively
invalidate completed Ws. sandbox/ work stays exempt (P8). This file changes
only with operator approval (same rule as GUARDRAILS).
