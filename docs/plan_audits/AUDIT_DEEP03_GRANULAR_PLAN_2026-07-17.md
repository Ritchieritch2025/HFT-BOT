# Independent Audit Report — DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md

Archived verbatim from the zero-context independent auditor session, 2026-07-17.
Target SHA-256 (pinned & verified by auditor): 9a1b70f2b658cc4baf1ce8149b45b0dda980fb21576d04041164b6d45f39e7bf

---

**Auditor status:** zero-context, read-only. Target hash verified before reading: `shasum -a 256` = `9a1b70f2b658cc4baf1ce8149b45b0dda980fb21576d04041164b6d45f39e7bf` — **exact match** to the pinned hash. Branch = `plan-sports-market-dynamics-v2`, HEAD = `7197bec` — both match the values recorded in the plan header (plan lines 6–7).

## 1. Verdict

**PASS_WITH_EXPLICIT_BLOCKERS**

The plan is internally consistent, its authority model matches the repository's actual authority chain, and — unusually — **every one of its nine code-surface claims in §6.1 verified true at the exact cited lines**. All 51 audit questions answer as the plan expects (one expected-NO answers NO). The fee arithmetic (§12.4), the 23-candidate TRAIN count (§8.6), K=6 (§10.8), and the 8-cell CS family (§12.5) all reproduce by hand. No P0 findings. One P1 (an appeal to an operator instruction that exists in no file) must be fixed during release drafting, and the plan's own declared blockers (dirty fee facts, absent W-FS1 engine, absent scope rulings, sample far below gates) are confirmed real against the repo.

## 2. Findings

### P0 — none found.

No GUARDRAILS MUST violation. The plan advances no phase, authorizes nothing, spends nothing, and its fail-closed/pessimistic/log-odds/fee posture is compliant with GUARDRAILS Q1–Q9 (`docs/GUARDRAILS.md:56-79`).

### P1

**P1-1 — §14.2 cites an operator instruction that is not on file (E2 violation by citation).**
Plan lines 1415–1417: "…not an attempt to overrule the operator's earlier instruction that deep03 need not wait 20 days to start." I searched `docs/PLAN_SPORTS_TRADING_DECISIONS.md`, `docs/SESSION_LOG.md`, `docs/PLANS_LEDGER.md`, and `docs/research_reports/DEEP03_SPEC_NOTES.md`: **no such instruction is recorded anywhere.** The only durable statements lean the other way: `docs/SESSION_LOG.md:269` — "deep03 前置:≥20 个独立封存日期(当前 2)" — records the 20-date figure as a *prerequisite*, and the closest supporting text is a design memo, not a ruling (`docs/DESIGN_BACKTEST_GAP_MAP_2026-07-16.md` §4 G-1: "不需要等 20 天数据" — about building the tape adapter, not starting deep03). Under GUARDRAILS E2 (`docs/GUARDRAILS.md:86-88`) a chat-only decision does not exist. The plan's *operative* rules are safe regardless (§14.1 line 1395: exploration begins only "once separately authorized"; D3-W0 requires a durable ruling first), so this is a text-integrity defect, not an executable one — but it must not survive into a release. Fix: either the operator archives that instruction verbatim in PLAN_SPORTS_TRADING_DECISIONS.md, or the sentence is deleted (correction proposed in §4 below).

### P2

**P2-1 — §3.1 routing tree (plan lines 169–183) is incomplete for its stated purpose ("for overlap audit").**
(a) Pre-scheduled *one-sided* books priced 90–97c are unrouted: PM-OS takes 10–90c, CS takes 97–99c; nothing states 90–97c → NO_QUOTE. (b) CS-TW (§12.3, lines 1238–1240: a *two-sided* high-side ask at 97/98) does not appear in the tree at all, while the tree's "pre-scheduled-start + valid two-sided → DR3-PM-TS-01" branch and §8.2 (which has no price exclusion for PM-TS) both claim the same 97/98 two-sided state. C-10 (line 137) and §3.1's "never sum capacity or PnL" (lines 185–188) prevent double-counted economics, but the overlap-audit artifact itself should show the collision.

**P2-2 — C-01's conflict inventory (line 128) misses one location of the stale "maker 0 fee" claim: the authority ledger itself.**
`docs/PLAN_SPORTS_TRADING_DECISIONS.md:44-47` (D-4 archival note, non-ruling text) says "体育市场的费用护城河(maker 0 费 vs taker 全费)" — contradicted the same day by `config/kalshi_facts.yaml:53` (maker_rate 0.0175 on `quadratic_with_maker_fees`) and lines 73–75 (KXNBA/KXATPMATCH/KXWTAMATCH are exactly such series, VERIFIED-LIVE 2026-07-16). The plan supersedes the claim as found in `DEEP03_SPEC_NOTES.md:24` but should name the D-4 note too, so no future session cites the ledger's aside as fee truth.

**P2-3 — §6.1 row 2 citation off by one line.** The create call without `post_only` is `apps/tradingd.cpp:294-295` (`wire::order_json(m.intent)`); line 296 is the error branch. Substance of the claim is correct (`include/kalshi/wire.hpp:163` defaults `post_only = false`; only line 189 emits it when true). Cosmetic.

**P2-4 — §18 (lines 1689–1712) never names `docs/MASTER_SEQUENCE.md`.** Per the authority split (CLAUDE.md, operator ruling D-1), engineering/infra ordering is MASTER_SEQUENCE's jurisdiction. D3-W3/W4 are engineering-code work packages; the plan requires per-W releases (sufficient in practice) but should state how D3-W* interleave with the engineering queue.

**P2-5 — C-07 could cite the already-decisive authority.** `docs/PLAN_MM_TEST_PROGRAM.md:307` (FS-4 red-light: "公开成交 3,自己剩余 10:最多 partial fill 3") and line 298 (FS-3: fills bounded by public trade size) already mandate the plan's resolution; `sandbox/research/mm_sandbox.py:264-270` ("the full remaining order is the certified lower-bound fill even when the through-print itself is small") is confirmed to violate it. The plan's "resolve by test and authority before G3" is correct but the authority already exists.

## 3. Contradictions and missing prerequisites (all confirmed against repo)

1. **Fee facts live only in a dirty worktree** (C-08 confirmed): `git status` shows `M config/kalshi_facts.yaml`; the diff flips `verified: false → true` per D-4 (`docs/PLAN_SPORTS_TRADING_DECISIONS.md:10-50`) and is **uncommitted**, as are the sentinel test updates (`tests/test_research_metrics.py`, `tests/test_gate_metrics.py`). Plan §4.1.4's clean-commit gate is currently unmet.
2. **W-FS1 engine does not exist.** Confirmed absent: cancel/amend/reconcile in `ExecSink` (`include/kalshi/strategy.hpp:20` — `submit` only); persistent private order/fill state (`apps/tradingd.cpp:294-329` sends and records telemetry only); cancel-effective semantics (`include/kalshi/rule_engine.hpp:114` and `:168` erase resting entries at *intent*, not at effectiveness); slice-aware reservation (`include/kalshi/risk_ledger.hpp:159-162` trusts caller `reduces_risk`; `:194-205` one-shot settle); public-size fill bound (`mm_sandbox.py:264-270`).
3. **Terminal-state semantics incomplete in code**: `src/gateway.cpp:128-141` maps only created/open/active/paused/closed/determined/settled; no void/cancel/retire/postpone states — A09/D3-D10 prerequisite confirmed missing. Also fail-open defaults confirmed: book-delta side defaults YES (`src/gateway.cpp:83-84`); trade taker side retains default when both fields absent (`:119-121`).
4. **Scheduled-start source unproven**: `sandbox/research/workbench/app.py:621` — "time-to-event is unavailable because historical fact tickers do not join to a verified occurrence_datetime." The PM gate's scheduled-start-known-roots preflight has no demonstrated data path today.
5. **Sample far below every gate**: 2 published canonical dates (07-12/13, both PRIOR_EXPOSED via deep01/deep02 — `docs/SESSION_LOG.md:245-246, 269`), seals continuous 07-10..15 + 07-16; vs ≥20 dates / ≥200 roots (`docs/SPORTS_AUTORESEARCH_02_AUTHORIZATION_2026-07-15.md:62`) and 7-clean-days/5-of-7 (D-1, `PLAN_SPORTS_TRADING_DECISIONS.md:160`).
6. **No durable scope rulings exist** for PM-OS / T90 / T90-RB / CS: V2.2 primary is two-sided pre-match only (`docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md:203`); Track D is `PROPOSAL_ONLY / OPERATOR_TBD` (`:348, 1005-1010`). Plan's BLOCKED_AUTHORITY labels are accurate.
7. **Repo-wide status caveat**: latest session (`docs/SESSION_LOG.md` 2026-07-17 entry) records readiness = 76 pass / 1 fail, overall `LIFECYCLE FAIL` (official OpenAPI/AsyncAPI hash change awaiting human review). Consistent with plan §18.1's "make check … necessary but not sufficient."
8. **Latency values are placeholders**: `config/backtest_latency.yaml:17-19` — 2 of 3 values `UNMEASURED_CONSERVATIVE`; §6.7's NON-GATE + {5,50,500}ms matrix treatment is the correct handling and matches `DESIGN_BACKTEST_GAP_MAP` G-4.
9. **MM_ROADMAP `费用=0` phrase confirmed** at `docs/MM_ROADMAP.md:37` (C-02 accurate; correction correctly deferred out of this plan-only write).
10. **Fake-green failure class is real and recent**: `docs/plan_audits/DETECTION_RUN_2026-07-17.md:11, 355` (make-check exit-code masking; "77/77" suite-identity fake green). The plan's §4.2 NOT_ESTIMABLE preflight, §16.1 declared-vs-executed split, and §17 atomic `RUN_COMPLETE` + stale-`latest.json` rejection directly target it.

## 4. Exact proposed text corrections (NOT applied)

1. **Plan lines 1415–1417**, replace:
   > "This section is an explicit audit target, not an attempt to overrule the operator's earlier instruction that deep03 need not wait 20 days to start."
   with:
   > "This section is an explicit audit target. Exploratory/engineering work before 20 qualified dates is permitted only by a separately authorized release (§14.1); no chat-only instruction is citable authority until archived verbatim in PLAN_SPORTS_TRADING_DECISIONS.md."
2. **Line 128 (C-01)**, append after "`DEEP03_SPEC_NOTES` says 'maker 0 fee'":
   > "; the same stale phrase also appears in the D-4 archival note (PLAN_SPORTS_TRADING_DECISIONS.md, 顺带发现 paragraph) and is equally superseded"
3. **§3.1 routing tree (lines 176–178)**, replace the one-sided branch with:
   ```
   pre-scheduled-start + valid one-sided
       +-- normalized price 10–90c -> DR3-PM-OS-01
       +-- high-price side 97–99c -> DR3-CS-ASK-01 / OS regime
       +-- 90–97c exclusive       -> NO_QUOTE (unrouted by design)
   ```
   and add under the two-sided branch:
   ```
   pre-scheduled-start + valid two-sided + high-side ask in {97,98}
       -> overlap: PM-TS and CS-TW both claim this state; families run
          separately, shared root-event cap, PnL/capacity never summed (C-10)
   ```
4. **Line 410 (§6.1 row 2)**: change "`apps/tradingd.cpp:294-296`" to "`apps/tradingd.cpp:294-295`".
5. **Line 134 (C-07)**, append: "(authority already decides this direction: PLAN_MM_TEST_PROGRAM FS-3 fill-size bound and FS-4 red-light 'public 3 / remaining 10 → max partial 3')".
6. **§18 (after line 1712)**, add: "Engineering-code work packages (D3-W3/W4-class) enter execution through the `docs/MASTER_SEQUENCE.md` engineering queue discipline (one W per fresh session, independent audit per W); their releases name both the deep03 dependency and the queue position."

## 5. Card-by-card table

| Card | Estimable now? | Blocked on | Required data | Legal verdict today |
|---|---|---|---|---|
| DR3-PM-TS-01 | Descriptive only (opportunity atlas, quote economics, markout) on 07-12/13 as EXPLORATORY_ONLY/PRIOR_EXPOSED, after D3-W0/W1 | Clean fee commit (dirty diff confirmed); W-FS1 engine (confirmed absent); scheduled-start source (confirmed unjoined); payout/lifecycle semantics (gateway enum incomplete) | ≥20 sealed dates, ≥200 roots, scheduled-start-known roots, fee-known rows | `BLOCKED_DEPENDENCY` — matches plan §4.3 |
| DR3-PM-OS-01 | Censor-corrected duration/incidence map only (deep02's duration defect confirmed as design target, §5.3) | Durable one-sided scope ruling (none exists); OS0 synthetic fixture; W-FS1 | Same as PM-TS + onset-anchor coverage | `BLOCKED_AUTHORITY` — matches |
| DR3-T90-01 | Past-only episode counts / tug / L2 path diagnostics (48.9M L2 rows, 485 markets on prior-exposed dates; 07-14 L2 partially quarantined: l2_00 excluded, v3-accepted l2_01..23 only) | Durable post-scheduled ruling; sequence-valid L2 admission; full order engine; terminal semantics | Sealed L2 dates + rule-semantics-complete rows | `REGISTERED` + scope `DIAGNOSTIC` — matches |
| DR3-T90-RB-01 | Recovery-state frequency counts only | Parent lock + own naming in a ruling; zero-resting reconciliation engine | Parent's data + burst/recovery episodes | `BLOCKED_AUTHORITY` — matches |
| DR3-CS-ASK-01 | Price/regime opportunity + queue-capacity upper bounds only | Durable Track-D ruling (V2.2:348 confirms PROPOSAL_ONLY); separate registry/split; 100% terminal outcomes (no void/retire path in code today) | Terminal-complete roots; upset support for power | `BLOCKED_AUTHORITY` — matches |
| DR3-CS-BID-CTRL-01 | Spec only; nothing to compute without ASK fills | Inherits all ASK blockers; control-only, non-promotable | Exact ASK fill set | `BLOCKED_AUTHORITY` — matches |

Plan §4.3's own capability table is honest and consistent with the repository; its statement "no card can yet legally emit BACKTEST_CANDIDATE" is correct.

## 6. The 51 Audit Questions — answers with evidence

Plan lines cited as "P:n"; expected answers all confirmed.

**Authority/scope:** 1 YES (P:13-17, 100-112, 1259-1263) · 2 YES (P:1402-1405; deep02 authority was single-run, $5/6h, W09-stop-at-end — AUTHORIZATION_2026-07-15 final block; W09 confirmed powered off, SESSION_LOG:271) · 3 YES (AGENT_OPERATING_MODEL_2026-07-17.md:3-4 "Status: WITHDRAWN"; not in plan's references P:1935-1955) · 4 YES (P:87, 202; RFQ = DATA_INTEGRITY_BLOCKED per SESSION_LOG 07-15) · 5 YES (P:88, 825-827; D-1 "外部工具全部暂缓…逐项单批", DECISIONS:142) · 6 YES (P:131, 147-155; V2.2:203 two-sided primary; V2.2:348 Track D locked).

**Sample/stats:** 7 YES (P:1398-1413; sources: AUTHORIZATION:62 vs DECISIONS:160; "never choose the best seven afterward" P:1408-1410) · 8 NO — as expected (P:1411 "never fewer without an explicit ruling") · 9 YES (P:1379-1386) · 10 YES (P:222-224, 1436-1437) · 11 YES (P:1440-1448) · 12 YES (P:1462-1464, 1478-1483) · 13 YES (P:1526-1537, 1599-1602).

**Fees/time/terminal:** 14 YES (P:194-196, 574-617; formula matches kalshi_facts.yaml:44 and fee_rounding.md; the $0.3301×0.03 golden example exists at fee_rounding.md:100-110) · 15 YES (P:128; stale claim confirmed at DEEP03_SPEC_NOTES.md:24 — with P2-2 caveat re DECISIONS:44-47) · 16 YES (P:132, 266, 346) · 17 YES (P:132, 266, 1069) · 18 YES (P:133, 1814-1816) · 19 YES (P:133, 221, 1231-1232) · 20 YES (P:268, 1296-1297, 1761).

**Overlap:** 21 YES (P:143-165) · 22 YES (P:150, 955-964) · 23 YES (P:1091-1103, 1146-1153) · 24 YES (P:154, 1218-1221, 1313-1317) · 25 YES (P:137, 185-188 — with P2-1 routing caveat) · 26 YES (P:157-165).

**Execution realism:** 27 YES (P:562-567, 1338; conflict with mm_sandbox.py:264-270 explicitly registered as C-07/P:134) · 28 YES (P:466-472) · 29 YES (P:700-704, 663-665) · 30 YES (P:170-171, 558-559, 897-904) · 31 YES (P:527-535, 679-685) · 32 YES (P:117-119, 784-791, 1168-1169).

**Reporting/decision:** 33 YES (P:1488-1524) · 34 YES (P:389-395) · 35 YES (P:60-63, 1478-1480, 1581-1584) · 36 YES (P:1526-1534, 705-707).

**Implementation/receipts:** 37 YES (P:409, 421-427; verified: strategy.hpp:20, tradingd.cpp:294-329) · 38 YES (P:410; verified: wire.hpp:163 default false, tradingd.cpp:294-295 no override) · 39 YES (P:413; verified: rule_engine.hpp:114, 168) · 40 YES (P:414-415; verified: gateway.cpp:83-84, 119-121) · 41 YES (P:539-545, 1737-1740) · 42 YES (P:1545-1565) · 43 YES (P:1563-1565, 1634-1635, 1651-1652) · 44 YES (P:1345-1360, 1766-1767) · 45 YES (P:498-507) · 46 YES (P:527-535, 1734-1736) · 47 YES (P:645-665) · 48 YES (P:980-982) · 49 YES (P:1264-1273; all 12 table cells recomputed by this auditor from maker_rate 0.0175, ceil-to-$0.0001, $0.01 non-direct floor — all correct, including 99c non-direct BID "no positive region") · 50 YES (P:1660-1666) · 51 YES (P:1619-1623, 1714-1716).

No question answered against the plan's stated expectation; therefore no automatic REVISE/REJECT trigger under P:1895-1896 fires.

## 7. Can it again produce empty/stale tests while appearing green?

The specific historical failure modes — registered-but-empty tests (deep01), whole-day one-sided duration extension (deep02, corrected at P:370-379), and fake-green harness results (DETECTION_RUN 07-17) — each have a named, mechanical countermeasure: §4.2 estimability preflight with `NOT_ESTIMABLE` stop, §5.3 right-censoring rule, §16.1 declared/executed split, §17 item 23 atomic-last `RUN_COMPLETE` with stale-pointer rejection. Design-level answer: no, provided the receipts are implemented as specified; that implementation is itself gated (D3-W1/W4, unbuilt), which is why the verdict carries explicit blockers rather than a clean pass.

## 8. Confirmation of no project changes

This audit made **no changes**: no file written or edited, no make targets, no tests, no research runs, no holdout opened, no W09 access, no production touch, no orders. Shell usage was limited to `shasum`, `git log/status/diff` (read-only), `grep`, `sed -n`, `head`/`tail`, `ls`, `wc`, and file reads. The working tree's pre-existing modified/untracked files were left exactly as found.
