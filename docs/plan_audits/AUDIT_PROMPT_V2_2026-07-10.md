# AUDIT — CANONICAL PROMPT V2

- Date: 2026-07-10
- Audit mode: independent, read-only
- Target: `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2.md`
- Target SHA-256: `bc2fbf6562c19fabbb7ebd7e8c77f88a720d1b6a2a467b39fbffce996bb1f341`
- Verdict: `PASS WITH REQUIRED REVISIONS`
- Execution performed: none
- Repository mutation during audit: none; this file preserves the returned audit

---
PASS WITH REQUIRED REVISIONS

当前候选不是 canonical：它位于 `plan-live-validation-p0-p3`，HEAD=`1c93837a4422fe54717224d3ef9fe16bac0d1018`，相对 upstream ahead 51；V2 是未跟踪文件，SHA-256=`bc2fbf6562c19fabbb7ebd7e8c77f88a720d1b6a2a467b39fbffce996bb1f341`；另有未跟踪 `outputs/`，无 staged/tracked dirty。`tools.json` 实测 144 项，`check_registry.py` 只读运行通过。本审计未运行 `make check` 或 `tests/run_pipeline.sh`，因为它们会写 `build/`、`work/logs/` 和 `work/test_results.ndjson`，违反本次严格只读授权。

## P0 — 必须在 canonical commit 前修订

### P0-1：release 落盘存在循环授权，AUD01 也没有明确 release

V2 一方面禁止在 durable branch release 前任何写入（V2:40-42, 307-308, 2923-2934），另一方面又规定聊天记忆不能授权写入、release 必须先落盘（V2:413-439），但唯一 release 登记位置直到 branch approval 后才进入写 allowlist（V2:2665-2670）。因此操作员即使在当前会话明确批准，也没有合法的第一笔落盘动作。GUARDRAILS E2 要求决定进入文件（`docs/GUARDRAILS.md:86-88`），DECISIONS 也是唯一体育策略裁决台账（`docs/PLAN_SPORTS_TRADING_DECISIONS.md:3-6`），确认了该循环不是形式问题。

此外，立即执行指令只要求 release 授权 W01（V2:2927-2930），却随后让新会话直接执行 AUD01（V2:2935-2936）；而 release schema 要列明 W IDs、有效期和 session count（V2:426-439）。W01 release 不会自动授权 AUD01。AUD01 还被允许在只有 evidence commit 时开始（V2:2818），尽管 W01 acceptance 明确还要求 closure commit（V2:2799-2802）。

最小替换语言：

> **Bootstrap release receipt — sole exception.** A current explicit operator instruction that names `release_id`, branch, base commit, worktree, exact W IDs, exact writes and session count may authorize only: (a) non-destructive creation/switch to that branch at that base, and (b) verbatim archival and commit of that instruction as `docs/plan_releases/sports_trading_program/<release_id>.md`. Historical, summarized or remembered chat never qualifies. No STP-P00 evidence work may begin until the receipt commit hash is returned. The release must name `STP-P00-W01` and `STP-P00-AUD01` separately, or AUD01 requires a separate release. AUD01 may begin only after both W01 evidence commit A and closure commit B exist, STATE says `IMPLEMENTED_AWAITING_AUDIT`, and the final W01 Git state is recorded.

### P0-2：exact write allowlist 与强制测试、mirror 直接冲突

W01 的“Exact allowed writes”只列 docs 文件（V2:2665-2680），AUD01 也只列四个 docs 文件（V2:2820-2825），但两者都必须重跑测试（V2:2796-2798, 2838）。实际：

- `make check` 会创建/更新 `build/` 和 `build/scratch/`（`Makefile:44-51, 366-368`）。
- `tests/run_pipeline.sh` 明确创建 `work/logs/`、`build/scratch/`，截断并重写 `work/test_results.ndjson`（`tests/run_pipeline.sh:14-19, 24-36`）。
- pipeline 的第一项要求 binaries 已由 full `make` 构建（`tests/run_pipeline.sh:74-78`），但 V2 没要求先运行 `make all`。
- exit ritual 还强制写 Desktop docs mirror（V2:2858-2867；`CLAUDE.md:89-95`），该路径也不在 allowlist。

按现文执行，测试成功本身就是越权写入。

最小替换语言：

> **Derived test and mirror writes.** In addition to the enumerated evidence documents, W01 and AUD01 may write only `build/**`, `work/logs/**`, `work/test_results.ndjson`, test-owned OS temporary paths, and `/Users/ritcardo/Desktop/TradingSys Report/docs-mirror/**`, solely as side effects of the required build, tests and final docs mirror. These derived paths must never be staged or committed. Run `make all`, then `make check`, then `tests/run_pipeline.sh`, then the registry check. Snapshot the derived paths before and after; any other unexpected write stops the session.

### P0-3：当前 V2 会被 checkpoint 抢先提交，违反独立 archive commit

Checkpoint candidate 被定义为完整 BOOTSTRAP dirty set，只排除 outputs、`.claude`、credentials、cache 等（V2:346-354）。当前 dirty set 正好含未跟踪 V2，因此现文要求先把它作为 `pre-audit checkpoint` 提交（V2:358-365）；但下一节又要求只 stage prompt、作为独立 archive commit（V2:378-395），并再次规定 prompt archive 与 checkpoint 必须是不同 commit（V2:2871）。

最小替换语言：

> The checkpoint candidate set contains only pre-existing dirty paths explicitly enumerated in the operator release. It always excludes `active_prompt_path`, release receipts, `outputs/`, `.claude/`, credentials, caches and generated bulk data. A pre-existing untracked active prompt is recorded as `PROMPT_PREEXISTING_UNTRACKED=<sha256>` and is handled only by the immutable prompt-archive procedure; it is never included in the checkpoint commit.

### P0-4：144 项是真数，但不是完整 runnable/live-path inventory

V2 把 `tools.json` 称为 authoritative runnable registry（V2:1832-1833），P00 acceptance 也只要求覆盖 144 项（V2:2658-2663, 2789-2792），并把现有执行路径概括成两个（V2:2084-2103），再要求“no third live order path”（V2:2172-2175）。

实际存在安全关键盲区：

- `apps/live_e2e.cpp` 有独立 `main()`（`apps/live_e2e.cpp:138`），在 live mode 直接 place/cancel 真实订单（`apps/live_e2e.cpp:330-399`），但不在 `tools.json`、Makefile 或 CMake 中。
- `check_registry.py` 明说它假设 `apps/*` 都由 Makefile 覆盖（`tools/check_registry.py:17-25`）；实际 reverse scan 只扫 `tools/*.py`、`tools/*.sh`、`work/research/*.py`（`tools/check_registry.py:45-53`），binary coverage 又只从 Makefile targets 推导（`tools/check_registry.py:116-137`），所以该 live path 被稳定漏检。
- `preflight` 注册为 `network_read`，但 args 包含 `--order`，description 也承认会真实下单（`tools.json:67`；`apps/preflight.cpp:9-15, 123-124, 293-318`）。这违反 registry 注释的 “Ambiguous rounds UP” 和 GUARDRAILS E3（`docs/GUARDRAILS.md:89-91`）。
- 除两个 general engine paths 外，`bench_order`、`fill_test`、`panic --execute`、`preflight --order`、`live_e2e` 都是独立 order-transmission surfaces。`docs/ARCHITECTURE.md:28-42` 的“两路径”只适合描述 general engines，不是完整 live-capable census。
- 构建 parity 也已漂移：Makefile 包含 `test_gold_layout`、`test_risk_ledger`、`test_rule_engine`（`Makefile:24-29`），CMake test list 没有它们（`CMakeLists.txt:65-80`），与 ARCHITECTURE 的 both-build-systems 规则（`docs/ARCHITECTURE.md:140-147`）不符。

最小替换语言：

> `tools.json` is the intended registry, not presumed complete. STP-P00 must perform a bidirectional census of: every registry entry; every Makefile/CMake target; every `main()` under `apps/` and `tests/`; every executable/script entry point; every code path containing authenticated mutation or order submission; and every registry argument mode. Record `REGISTRY_COUNT_OBSERVED` dynamically; 144 is generation-time evidence only. Any action mode that can mutate or transmit is `live_order` unless split into a separately registered safe entry. The repository currently has two general engine paths plus standalone live-capable utility/emergency paths; P00 must enumerate all of them. “No third live order path” means no new strategy transmission architecture. It does not erase existing utilities or the independently operable S3 panic path, all of which must reuse audited submission/reconciliation primitives or remain `DO_NOT_USE`.

Acceptance 中的 “all 144” 应替换为：

> all `REGISTRY_COUNT_OBSERVED` entries and all filesystem-discovered runnable/live-capable entry points covered; count drift and registry/build/safety mismatches surfaced.

## P1 — 方法与治理必须修订

### P1-1：D-1 引用的是缺失旧 prompt，必须有 D-2 映射；Track D 也未经 D-1 裁决

D-1 的操作员原文引用旧 `CANONICAL PROMPT §5` 和 `§11`（`docs/PLAN_SPORTS_TRADING_DECISIONS.md:12-17`），并明确说旧全文尚未入库、Phase 1 前不得靠记忆解释（同文件:36-40）。V2 自己也承认不得重构 v1，缺失 v1 是 P01 blocker（V2:397-400）。V2 章节号已变化，因此不能静默把 D-1 的旧 §5/§11 当成 V2 同号章节。

所需 D-2 精确映射：

- D-1 旧 §5“主线定义” → V2 §2，尤其 V2:119-159；以及 V2 §11.2–§11.4，尤其 V2:801-850。
- D-1 旧 §11“OPERATOR-TBD 提案机制” → V2 §4 conflict rule，V2:241-246；以及 V2 §10，尤其 V2:575-622、698-775。
- 该映射只解释 D-1，不恢复或伪造丢失的 v1。

V2 另加 Track D（V2:843-850），而 D-1 只裁决 A/B/C（DECISIONS:14-16, 27-30）；Track D 必须明确 proposal-only。

最小替换语言：

> Before STP-P01, obtain operator decision D-2 that either recovers the authoritative v1 full text or maps D-1’s old references as follows: old §5 → V2 §2 and §11.2–§11.4; old §11 → V2 §4 conflict rule and §10. D-2 is an interpretation map only and does not reconstruct v1. Until D-2 exists, P01 remains blocked. Track D is `PROPOSAL_ONLY / OPERATOR_TBD`; it is not part of D-1 and cannot enter any candidate registry without a new release.

另一 agent 提出的“先把 `plan-live-validation-p0-p3` merge 到 main 再建 sports branch”不是 D-2，也不是 V2/P00 的一部分。它是独立的 51-commit repository-integration action；V2 明禁 merge（V2:61, 2873），并要求只用操作员指定 base、不得静默使用现有工程分支（V2:332-341）。当前 `main=92918bb`、sports candidate base=`1c93837`；是否整合必须另开工程 W、独立审计和 operator release，不能夹带进 prompt archive 或 P00。

建议在 Branch 段加入：

> Selecting a program base does not authorize merging any engineering branch into `main`. Repository integration is a separate engineering W and release.

### P1-2：DECISIONS 只应记录操作员裁决，不能写 agent audit status

DECISIONS 文件定义为操作员裁决的权威台账、新条目在上（`docs/PLAN_SPORTS_TRADING_DECISIONS.md:1-6`）。V2 却允许 W01 写“release/audit registration”（V2:2669-2670），AUD01 也可写 audit status（V2:2822-2825）。独立审计结论是证据，不是操作员裁决；放进唯一策略裁决台账会制造伪权威。“append-only”还与该文件的 newest-first 布局语义不清。

最小替换语言：

> `PLAN_SPORTS_TRADING_DECISIONS.md` receives only verbatim operator rulings and releases. Agent audit findings or PASS/REVISE/REJECT status live in the audit artifact, STATE and SESSION_LOG; they enter DECISIONS only if later adopted verbatim by the operator. “Append-only” means add-only at the ledger’s documented newest-first insertion point: existing entry bytes and their relative order never change.

### P1-3：PnL 公式仍需显式 balance-sheet identity，避免 settlement/TerminalValue 重复

V2 的方向是正确的：cash flows 一次、attribution 不是额外扣减（V2:1247-1297）。但公式同时包含 settlement cash flow（V2:1266-1267）和 remaining-inventory `TerminalValue`（V2:1254-1260），没有明确 settled lot 的 TerminalValue 必须为零，也没有把 free cash、segregated collateral、liability 和内部 collateral transfer 写成完整财富恒等式。

仓库里 `PriceE4` 明确禁止用于 fee/notional/balance/PnL（`include/trading/fixedpoint.hpp:7-13`）；目前可复用的货币类型是 risk ledger 的 E6 micros（`include/kalshi/risk_ledger.hpp:3-11, 57-68`），但尚无完整 PnL ledger。

最小替换语言：

> Define event wealth as `W_t = free_cash_t + segregated_collateral_t + V_t(open_position_lots) - liabilities_t`, all in one audited fixed-point money type distinct from `PriceE4`. Define `NetPnL_e = W_T - W_0 - net_external_contributions - distinct_non_venue_variable_costs`. Transfers between free cash and collateral are zero-PnL. Each trade, fee, unwind and settlement cash flow enters exactly once. `V_T` applies only to still-open lots and is zero for lots already liquidated or settled. The binding residual value is the minimum over feasible joint settlement states of all linked contracts under the frozen terminal policy.

### P1-4：sealed split 缺少显式 contaminated/exploratory assignment

V2 只让 manifest 分配 TRAIN、VALIDATION、HISTORICAL_CONFIRMATION（V2:1343-1350），随后才说此前看过的期间属于 EXPLORATORY_ONLY（V2:1360-1372）。仓库已经有被查看的 S1 结果和日期（`docs/PLAN_SPORTS_TRADING_DECISIONS.md:24-26`；`docs/SESSION_LOG.md:63-68`）。若 split generator 没有第四种强制标签，已暴露 event 仍可能被机械分进 sealed sets。

最小替换语言：

> The split manifest has four mutually exclusive assignments: `EXPLORATORY_ONLY`, `TRAIN`, `VALIDATION`, and `HISTORICAL_CONFIRMATION`. Before chronological assignment, join the prior-exposure ledger and force every root event whose price, feature, fill, markout, outcome, selection or PnL summary was previously exposed into `EXPLORATORY_ONLY`. Hash-seal both the eligibility population and assignment. No exposed event may enter VALIDATION or HISTORICAL_CONFIRMATION. If untouched history is inadequate, confirmation accrues prospectively.

### P1-5：cancel competing risks 需要 multistate/recurrent 定义；calibration 缺少代表性与功效门

V2 正确拒绝独立 percentile 比较（V2:1163-1190），但它列出的“safe cancel / fill before / during / after ack”等 outcome 不是天然互斥：partial fill 后仍可能安全撤掉 remainder，late private update 也可能把 exchange-time fill 误报成 receive-time post-ack fill。Simulator 又明确要求 partial/repeated fills（V2:1588-1627）。

Calibration 虽要求记录 inclusion probability 和 disjoint cohorts（V2:1642-1647），但未要求目标部署分布、positivity/weighting、adequacy sample size/power；仅冻结 metrics/tolerances（V2:1648-1662）不足以证明代表性。

最小替换语言：

> Model cancellation as a multistate, recurrent process, not one mutually exclusive episode label. Freeze the time origin, exchange-event clock, local-receive clock, risk set, censoring rule, cause-specific cumulative-incidence estimands, cumulative filled size, remaining size and terminal state. A fill reported after cancel acknowledgment is classified by matching-engine time when available; otherwise it is ambiguous and resolved conservatively.
>
> Before calibration begins, freeze the target deployment opportunity distribution, inclusion mechanism, inclusion probabilities, positivity/support checks, weighting or conditional target, minimum calibration and validation sample sizes, simulator-adequacy margins, alpha/power and regime coverage. Passing simulator fit on a selectively sampled cohort cannot validate deployment-wide fill probabilities.

### P1-6：legacy reuse map 漏掉已知的关键限制

V2 对 `mm_backtest` 已列 immediate requote、full fill、cancel lifecycle、placeholder latency（V2:2016-2033），但源码还有更关键的 gate-invalid limitations：

- maker fee 硬编码为 0、无 settlement、末尾 inventory 按 last mid（`tools/mm_backtest.py:16-20`）；
- cash/inventory 用 float（同文件:105-151），违反 D5；
- 旧 quote requote 时立即消失（同文件:43-57, 116-123）。

Maker-edge 也不是 lifecycle simulator：

- 使用 `ts_utc` 做 ASOF（`tools/research/maker_edge_pilot.py:76-112, 123-153`）；
- 把每个 public trade 相对 contemporaneous touch 直接分类成 optimistic/pessimistic（同文件:172-190），没有 quote decision、activation、cancel、inventory 或 cash ledger；
- `event_id` 只是去掉 ticker 最后一段的 regex（同文件:190）；
- primary metric 是 fill-conditional half-spread-minus-drift，而不是 root-event NetPnL（`tools/research/aggregate_maker_edge.py:164-208`）。

最小替换语言：

> Additional verified `mm_backtest.py` limitations: floating-point cash/inventory; maker fee hard-coded to zero; no settlement; residual inventory marked at last midpoint; and immediate stale-quote removal. Additional verified maker-edge limitations: exchange-time ASOF analysis; public-trade touch-cross classification without an own-order lifecycle; no inventory, cash, terminal or zero-fill-event ledger; and heuristic ticker-based event IDs. These components and results are `HISTORICAL / DIAGNOSTIC_ONLY`; orchestration/reporting may be reused only after component-level separation.

## P2 — 应清理但不单独阻止 archive

- `LCB_97.5(mu_event)`（V2:1422-1425）容易被误读。建议写成“lower endpoint of a two-sided 95% CI, equivalently a one-sided 97.5% lower confidence bound”，并固定一个机器字段名，例如 `ci95_lower`.
- “read completely where relevant”（V2:2636）自相矛盾。指定 mandatory docs 应写 `read completely`; phase-critical source 才可用 `relevant sections`, 并在 READ MANIFEST 记录范围。
- Saved official Kalshi spec 足以支持 P00 的 docs-level capability inventory：tracked manifest 给出 official URLs/hashes（`docs/vendor/kalshi/latest/manifest.json:4-43`），OpenAPI 包含 batch/cancel/amend/decrease 与 queue consequences（`docs/vendor/kalshi/latest/openapi.yaml:1057-1217`）以及 TIF/post_only/reduce_only/STP/pause fields（同文件:6796-6838）。但 P00 只能标 `SAVED_SPEC_VERIFIED`，不能把它升级为 `VENUE_VERIFIED`；没有必要为本次只读 prompt 审计联网。

## 已经很强、不能削弱

- 严格 phase ceiling：只到 P00，P01+ 和 live/external/production 全部禁止。
- GUARDRAILS 与 D-1 保持 binding，所有 rewrite 都明确 proposal-only。
- D-1 的 7 clean days、5-of-7、25%、×1.5/×2 继续 provisional，而不是被“功效分析以后再说”静默暂停。
- Strict-through 的核心定义很强：activation latency、cancel-pending exposure、at-price 不成交、same-timestamp adverse、gap fail-closed、一张容量，以及“不是数学 PnL lower bound”的诚实限定。
- Pathwise cash accounting与 attribution 分离，明确禁止 double count。
- Root-event 聚合、zero-quote/zero-fill 进入样本、早封存、nested TRAIN、一次 VALIDATION、一次 confirmation、multiplicity 和 sequential-testing 纪律。
- Simulator calibration、disjoint validation、post-freeze prospective cohort 三者分离。
- 一张证据只支持一张 capacity、禁止线性外推、scale 必须新 ladder。
- REUSE/EXTEND/DIAGNOSTIC/PRESERVE/MIGRATION 分类体系、同一 decision core、生产 data path PRESERVE。
- Evidence commit A + closure commit B 避免 self-referential hash 的协议。

## 最终建议

不得把当前 SHA 的 V2 直接提交为 active canonical。先修订以上 P0/P1，并取得 D-2 映射与非循环 release receipt；merge-to-main 提案必须完全分离，不能作为前置动作偷偷执行。

由于当前候选 SHA 已被公开记录、且文本自称 immutable，最稳妥做法是保留本候选作为审计过但未接受的 artifact，操作员批准后生成 `V2.1` 新路径和新 SHA。若坚持修改当前未提交文件，也必须由操作员明确批准“pre-archive rewrite”；不得静默覆盖。只有修订版通过再次独立只读审计后，才可在操作员指定 branch/base 上作为 canonical commit。

