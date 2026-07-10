# PLAN — Risk Gates & Kill Switch (MM_ROADMAP Phase 3 preparation)

**For the executing agent: read `docs/GUARDRAILS.md` and `docs/MM_ROADMAP.md`
before touching anything. This plan binds to them; a conflict means STOP and
ask the operator.**

- **Phase advanced:** 3 preparation (live-safety gates), buildable in
  parallel with Phase 2 per MM_ROADMAP — but every W here is dry-run /
  synthetic / read-only; the single live-rehearsal W is OPERATOR-GATED
  (S1/S3) and sits at the very end.
- **Gates:** P1 (phase named); P2 (nothing here places a live order without
  the full S1 stack + operator per-session confirmation); P3 (bounded Ws,
  demonstrated acceptance, rollback per W); P4 (pipeline untouched — the
  kill switch is a standalone process, S3); S2 fail-closed everywhere; S5
  (console never runs live_order class); S6 (reserve-before-send is the
  ledger built here); E1/E2/E3/E5.
- **Safety classes:** W-K1 `network_read` (typed read-only endpoints);
  W-K2 dry-run mode `offline` (mock exchange) with the real-execution path
  registered `live_order` (console-forbidden forever, E3/S5); W-K3/K4/K5
  `pure`/`offline` synthetic; W-K6 = operator-gated live rehearsal.
- **Design input (binding):** `docs/DESIGN_HOTPATH_EXECUTION_2026-07-10.md`.
  Its nine hot-path contracts, five-layer reservation ledger, and four named
  acceptance tests are folded VERBATIM in §1 below and bound into the W
  definitions. **Omitting any of them from the built system = this plan's
  self-audit FAILS (§6.11) and the W audit REJECTS.**

---

## 0. Problem statement

GUARDRAILS S3: **the kill switch is built and rehearsed before the first
live order** — a standalone process independent of the strategy (panic CLI:
cancel-all → verify zero resting → reprice-cross liquidation rounds →
report). MM_ROADMAP Phase 3 lists the live gates: risk caps, kill switch,
reconcile-on-ambiguity, token budget, funding, per-session operator
confirmation. None of these exist as code today; the interview archive's two
flagship account-blowups (Eggsy correlated-lines, NHL template legs) are both
*factor-level* exposure failures that per-market caps alone cannot stop.

This plan builds the safety stack in dry-run form NOW, so that when Phase 2
shadow ends, live-readiness is a rehearsal away — not a development project.

## 1. Design contracts folded VERBATIM (from DESIGN_HOTPATH_EXECUTION_2026-07-10.md §4–5)

> ## 4. 热路径契约(九条,设计定稿输入)
>
> 操作员六条:
>
> 1. **订单状态常驻内存**:当前仓位、活跃订单、各 market 最佳
>    bid/ask、风险额度,策略判断即取即用。
> 2. **订单模板预建**:market、buy/sell、yes/no、post_only 等固定
>    字段提前放好;下单只改价格、数量、client_order_id、timestamp。
> 3. **固定内存槽位**:预分配一批 OrderSlot,不做临时对象;策略
>    拿槽位填关键字段。
> 4. **热连接常开**:每个下单 worker 自有 warm HTTP/TLS lane,
>    绝不临下单才建连接。
> 5. **签名不可省**:Kalshi 每请求需新 timestamp + RSA-PSS 签名,
>    每单必做;可并行、预分配 buffer、减少额外分配。
> 6. **Redis 不入热路径**:日志/telemetry/cold path 可用;真正
>    下单路径全程同进程内存。
>
> 补充三条(缺一即经典实盘事故源):
>
> 7. **额度是预留式,不是查询式(reserve-before-send,S6)**:
>    策略取槽位那一刻即从额度扣除本笔敞口 → 发送 → ack 按实结算,
>    失败/超时归还;部分成交结算成交部分、归还剩余。只"看一眼"
>    额度 = 同毫秒两笔各自看到余额双双发出 = Q8 multi-fill 窗口
>    bug 族。
>    **账本必须开五层,取槽位时五层原子同扣,任何一层不足即拒单:**
>    ① 单市场 ② 单 event ③ 单因子(同一底层标的/同一结果方向,
>    如同场比赛的全部 alternate lines、全部 BTC brackets)
>    ④ 总敞口 ⑤ 日亏损。
>    (路线图阶段 3 现有清单只有 ①④⑤+单笔,②③ 为本设计新增,
>    正是访谈两大事故的病根层。)
>    **减风险方向的单不占额度、永远放行**(Q8 rodlaf 死锁:满仓时
>    禁止逻辑上压制退出报价)。
>    **验收测试点名(红字先行,进 PLAN_RISK_KILLSWITCH 的 W 定义,
>    缺一即计划审计 REJECT):**
>    (a) Eggsy 重放:同一 event 十条相关线同毫秒并发下单,
>        因子层额度只放行第一笔预留,其余九笔拒单;
>    (b) NHL 模板重放:同一条腿串进 N 张不同组合,腿级因子敞口
>        聚合并触顶;
>    (c) 死锁豁免:因子层满额时,减仓方向报价仍被放行;
>    (d) 归还路径:发送失败/超时/部分成交后账户余额精确复原
>        (E4 定点数,无浮点)。
> 8. **内存是缓存,交易所是真相——必有对账回路(S2)**:冷路径
>    定期拉交易所 resting orders/positions 与内存对表;ack 丢失、
>    部分成交漏收、断线窗口都会造成漂移;不一致 → 报警 + 以
>    交易所为准,永不盲目重试。
> 9. **client_order_id 承担幂等**:重试必须复用同一 id(换 id
>    重发 = 可能双成交);生成规则(进程内单调、含槽位号)在
>    设计里写死。
>
> ## 5. 排序纪律(与 GUARDRAILS 对齐,防跑偏)
>
> - **安全层先于速度层**:kill switch 先建先练才许第一笔实盘
>   (S3);post-only、dead-man、下单前三查(仓位/敞口/日亏)、
>   reserve-before-send 是下单路径的准入条件(S6),不是 v2 功能。
> - **先测量再优化**:延迟大头是签名(数百 µs 量级)+ signed POST
>   RTT(ms 量级);OrderSlot 池省的是 ns-µs。签名与 RTT p99 实测
>   先行(W-TL1 占位参数的后续任务),数字出来后工程往大头砸。
> - **第一版 edge 不靠极速**(访谈档案结论):maker 吃费率墙 +
>   散户流,防快人靠报价熔断与 size 控制;PrivateLink/多 AZ 等
>   放量且被延迟真实咬过之后再投。
> - 遥测/日志/S3/dashboard 永远滚出热路径(S5/E7)。

Contract-to-W binding map (every contract has an owner; §6.11 checks this):

| contract | owned by |
|---|---|
| #1 in-memory order/position/risk state | W-K3 (ledger state) + Phase-2 engine plan (book/orders) |
| #2 prebuilt templates, #3 OrderSlot pool | future execution-engine plan (DESIGN §6 悬置); NOT this plan — recorded so the audit sees it deliberately deferred, not lost |
| #4 warm lanes, #5 per-order RSA-PSS | existing `client.hpp` Lanes + signing path; panic CLI reuses them (W-K2) |
| #6 no Redis on hot path | W-K3/K4 are in-process pure modules; enforced by their Forbidden-writes |
| #7 reserve-before-send + five layers + (a)–(d) | **W-K3 (the heart of this plan)** |
| #8 reconcile loop | W-K5 |
| #9 client_order_id idempotency | W-K2 (panic reuses ids on retry) + W-K3 (id rule spec + tests) |

## 2. Module architecture

```
include/kalshi/risk_ledger.hpp   W-K3  five-layer reservation ledger (C++,
                                       hot-path destined, E7; E4 fixed point)
tests/test_risk_ledger.cpp       W-K3  the four named tests + property battery
apps/panic.cpp                   W-K2  standalone kill-switch CLI (S3)
tests/test_panic_dryrun.py       W-K2  dry-run vs mock exchange
tools/account_view.py            W-K1  typed read-only endpoints (positions/
                                       resting orders/balance) — panic's and
                                       reconcile's data source
include/kalshi/dead_man.hpp      W-K4  rule engine primitives (dead-man,
tests/test_rule_engine.cpp             day-loss breaker, rate limiter)
tools/reconcile.py               W-K5  cold-path exchange-vs-memory diff
```

C++ for hot-path-destined pieces (E7); Python for cold-path/ops tooling.

---

## 3. Workstreams (seven-field Ws)

### W-K1 — Typed read-only account endpoints
Purpose:          `tools/account_view.py` + (if needed) C++ typed parsers:
                  GET portfolio positions, resting orders, balance — typed,
                  boundary-validated (D3), read-only. This is the data source
                  for panic (verify-zero-resting), reconcile (W-K5), and the
                  operator's own eyes. Endpoint paths/fields verified against
                  docs/vendor spec + live capture (E4-discipline), never from
                  memory.
Allowed writes:   `tools/account_view.py`; typed structs/tests;
                  tests with recorded/mock fixtures; tools.json
                  (`network_read`).
Forbidden writes: any order-mutation endpoint code; pipeline; strategy code.
Acceptance:       mock-server fixtures (real captured response shapes) parse
                  to typed values byte-exactly (E4 money fields, no floats);
                  malformed/corrupt responses fail-closed with counted drops
                  (D3/D2); one read-only live call demonstrated (allowed —
                  read class) printing the operator's actual balance/orders.
Rollback:         revert commit.
Exit evidence:    commit hash; tests green; live read output (redacted ok).

#### W-K1 RESULT (2026-07-10 — DONE; audit report in docs/plan_audits/)
- Delivered: `tools/account_view.py` (balance / positions / resting orders;
  openssl RSA-PSS signing mirroring src/client.cpp; D3 field gates with
  counted drops; cents↔fixed-point cross-check fail-closed;
  `--assert-zero-resting` panic primitive, UNPROVABLE⇒exit 1 when any
  record fails gates) + `tests/test_account_view.py` (13 tests, local mock,
  PSS round-trip vs openssl verify, redaction + read-only grep gates).
- **DEVIATION from the W text, evidence-forced (D5):** the definition said
  "E4 money fields"; the LIVE account answered
  `market_exposure_dollars="4.726960"` — six decimals, nonzero beyond
  centicent — so E4 would be lossy narrowing of account money. Money is
  parsed byte-exactly to **E6 micro-dollars** (`parse_e6`, integer digit
  accumulation, floats rejected); contract counts stay E4 (spec pins
  FixedPointCount to 2dp). Both live shapes are committed fixtures.
- Field-semantics facts (VERIFIED-LIVE 2026-07-10): `balance` (cents int)
  = floor of `balance_dollars`; true balance carries sub-cent precision
  ($23.2614 while cents said 2326). W-K3's ledger (E4 per its definition)
  must adopt E6 for money or state its narrowing rule explicitly — flagged
  for the W-K3 session.
- Live demo (acceptance): balance $23.261400, portfolio value $4.70, 3/3
  market positions parsed (0 dropped — incl. a KXMVECROSSCATEGORY combo
  position, 25.69 contracts / $4.726960 exposure), resting orders 0,
  `--assert-zero-resting` rc=0.
- Independent audit: initial verdict REJECT (B1 pagination truncation was
  fail-open in the zero-resting primitive; B2 unicode digits mis-computed
  money) — both fixed same session + tests; redirects now refused (N1);
  balance cross-check widened to 1-cent tolerance (N2: floor vs round
  unresolved from one sample, both pass, >= 1 cent = corruption). Report:
  docs/plan_audits/wK1_audit_2026-07-10.md. CAVEATS for consumers (audit
  N4): GET /portfolio/orders defaults to ALL subaccounts (zero-resting
  verdict is account-wide, correct for panic) but GET /portfolio/positions
  defaults to PRIMARY only — W-K5 reconcile must pass subaccount params if
  subaccounts ever exist. Ignored-but-available fields (order_group_id,
  subaccount_number, expiration_time, balance_breakdown, …) listed in the
  audit for W-K2/K5 to adopt as needed.

### W-K2 — Panic CLI with dry-run mode (the kill switch, S3)
Purpose:          `apps/panic.cpp` (or panic.py if the audit accepts cold-path
                  Python for v1 — decision recorded in the W): a STANDALONE
                  process, zero strategy dependencies (S3), sequence:
                  cancel-all → poll until zero resting (W-K1 views) →
                  reprice-cross liquidation rounds for residual inventory →
                  final report. DRY-RUN default: full sequence against the
                  mock exchange, every would-be mutation printed + logged,
                  nothing transmitted. Real mode: `live_order` class,
                  console-forbidden (S5/E3), operator-invoked only. Retries
                  reuse client_order_id (contract #9); post-only NOT used for
                  liquidation rounds (crossing is the point) — flagged
                  explicitly so S6's post-only rule is read as maker-quote
                  scoped, with the operator confirming that reading at audit.
                  **OPERATOR RULING (2026-07-10, E2, verbatim): panic 清仓单
                  允许 crossing(吃单)——紧急退出要的是确定成交,不是好
                  价格,taker 费在 panic 语境下是可接受成本;每笔 panic 单
                  必须带 dead-man 到期(审计 N4 条款)。除 panic 外,一切
                  策略性退出维持 post-only 被动(费用自杀红线不变,
                  MM_ROADMAP 1.5B 原文)。**
Allowed writes:   `apps/panic.cpp`; `tests/test_panic_dryrun.py`;
                  `tests/mock_exchange_panic.py` (mock with seeded resting
                  orders/positions incl. ack-loss injection); Makefile;
                  tools.json (dry-run entry `offline`; live entry
                  `live_order`).
Forbidden writes: strategy/engine code; ws_shadow/supervisor; pipeline.
Acceptance:       dry-run against a mock seeded with N resting orders + M
                  positions: cancels all, verifies zero, plans liquidation
                  rounds with correct crossing prices (E4 hand-computed),
                  prints report; ack-loss injection ⇒ re-poll and reissue
                  with the SAME client_order_id (contract #9 proven); mock
                  refusing a cancel ⇒ loud partial-failure report, exit
                  nonzero (S2 — never a false "all clear", D2); panic's OWN
                  liquidation orders carry dead-man expiry (S6 second
                  clause: an unfilled cross must not become an orphaned
                  resting order — audit N4), asserted on the dry-run plan;
                  registry check proves the live entry is console-refused.
Rollback:         revert commit; dry-run has no external effects.
Exit evidence:    commit hash; dry-run transcript in the log; registry proof.

#### W-K2 RESULT (2026-07-10 — DONE; audit report in docs/plan_audits/)
- Language decision (recorded per the W text): **C++** (`apps/panic.cpp`) —
  reuses the proven client/env/wire layers (signing, warm lanes, redaction,
  the `require_orders_allowed` S1 choke point, fail-closed env
  cross-validation) instead of standing up a SECOND mutation-capable stack
  in Python; E7-consistent. account_view (Python) stays the read-only
  cross-check tool.
- Operator ruling (2026-07-10, E2, verbatim above): crossing allowed for
  panic, dead-man mandatory. Implementation: liquidation orders are
  `time_in_force=immediate_or_cancel` + `reduce_only=true` — IOC IS the
  dead-man (expiry immediate; structurally cannot become an orphaned
  resting order), reduce_only means panic can never CREATE risk (Q8). If a
  future panic version uses any resting order type, `expiration_time`
  becomes mandatory. The drill mock 400-rejects any order violating this
  contract, so every green drill re-proves it.
- client_order_id (contract #9): wire::client_order_id derives from
  (ts_ns, strategy_id, seq) ONLY (audit N3 correction) — run-stable ts +
  strategy_id=29 (reserved panic namespace) + per-intent seq ⇒ unique per
  intent, byte-identical on retry; ack-loss drills prove ZERO double fills
  against the mock. **ASSUMPTION (audit N1, requires LIVE confirmation at
  W-K6): the exchange dedupes orders by client_order_id and answers 409 on
  a retried id — openapi documents coid-dedup only for transfers.
  Worst-case bound if false: reduce_only caps at flat + post-round
  re-enumeration keeps the report honest.**
- Drills (8 tests, real binary vs seeded localhost mock): dry-run
  journal-proven silent; clean execute (hand-computed crossing prices:
  long→ask@bid, short→bid@ask, magnitudes from position_fp); cancel/order
  ack-loss recovery; refused cancel ⇒ loud PARTIAL-FAILURE exit 1;
  endless-cursor enumeration ⇒ INCOMPLETE, never "clear" (account_view B1
  rule); --execute vs prod-shaped env without live arming ⇒ refused (S1).
- LIVE dry-run demonstrated (read-only): orders=0, positions=0 — the real
  account was genuinely flat at run time (the W-K1-era MVE position had
  settled/closed; re-verified via raw GET: all position_fp zero). Also
  confirms simdjson handles the real API's alphabetical field order.
- Registry: `panic_dryrun` (network_read) + `panic_live` (live_order,
  console-forbidden — proven by check_registry output) + `test_panic_dryrun`
  (offline, localhost mock only).
- Independent audit: initial verdict REJECT — **B1: the mock-drill branch
  keyed on the env LABEL alone, so KALSHI_HOST_UNSAFE_OVERRIDE could aim a
  local_mock env at the real host and --execute would fire unarmed (S1
  defeated). Fixed over THREE audit rounds — the drill branch requires the
  RESOLVED base URL host to be loopback by a complete RFC-3986 authority
  parse (terminate at first `/?#`, strip userinfo at last `@`, strip port,
  `[::1]` brackets, case-fold), each round closing one more bypass class:
  substring (`localhost.evil.com`, round 1→2), userinfo (`127.0.0.1@evil`,
  round 2→3), query/fragment (`realhost?@127.0.0.1`, round 3). Regression
  tests cover all eight vectors. The env-layer parse_url shares the userinfo
  quirk ⇒ BACKLOG (panic's guard is fully independent of it).** Also
  applied: N2 empty-exit-side refusal (no fabricated 1c/99c dumps) + 400 no
  longer counts as cancel success (404 only); N3 id-derivation comment
  corrected; N4 cursor re-read uses a fresh simdjson parser (one-live-doc
  contract). 8→10 drills. FINAL verdict ACCEPT after the round-3 fix (auditor
  probed 18 authority-grammar corners + libcurl connect-target confirmation).
  Report: docs/plan_audits/wK2_audit_2026-07-10.md. Residual (non-blocking,
  both recorded): the airtight design is for resolve_runtime to expose ONE
  validated host the connection also uses so the drill gate never re-parses
  — future env-hardening W (BACKLOG); N1's 409-coid-dedup stays an ASSUMPTION
  for W-K6 live confirmation.

### W-K3 — Five-layer reservation ledger (contract #7, the heart)
Purpose:          `include/kalshi/risk_ledger.hpp`: in-process, in-memory
                  (contracts #1/#6), E4 fixed-point (D5, no floats on money),
                  atomic five-layer reserve at slot-take:
                  ① per-market ② per-event ③ per-factor (same underlying /
                  same outcome direction — same-game alternate lines, all-BTC
                  brackets) ④ total exposure ⑤ daily loss. ANY layer short ⇒
                  reject the order. Settlement on ack (actual fill part),
                  refund on failure/timeout/remainder. Reduce-risk orders
                  bypass reservation and are ALWAYS admitted (Q8). Factor-key
                  derivation rule is part of the W (event/series/strike
                  mapping table + explicit `unknown_factor` fail-closed class
                  that reserves against the MOST conservative bucket).
                  client_order_id generation rule (process-monotonic,
                  slot-indexed) specified + tested here (contract #9).
Allowed writes:   `include/kalshi/risk_ledger.hpp`;
                  `tests/test_risk_ledger.cpp`; Makefile; tools.json.
Forbidden writes: network code of any kind (this module must be linkable
                  with zero I/O deps — contract #6); strategy code; pipeline.
Acceptance:       **the four named tests, verbatim from the design doc,
                  each as its own named test case — absence of any one =
                  audit REJECT:**
                  (a) **Eggsy replay** — ten correlated same-event lines
                      submitted same-millisecond concurrently: the factor
                      layer admits exactly the first reservation, rejects
                      the other nine;
                  (b) **NHL template replay** — one leg threaded into N
                      different combos: leg-level factor exposure aggregates
                      across combos and caps out;
                  (c) **deadlock exemption** — factor layer at cap: a
                      reduce-direction quote is still admitted;
                  (d) **refund path** — send-failure / timeout / partial
                      fill each restore the account EXACTLY (E4 integer
                      equality, no float anywhere — grep-gate like WP-01).
                  Plus property battery: five-layer atomicity under a
                  multi-threaded hammer (no interleaving admits a breach —
                  the "look then send" bug class proven impossible);
                  reservation conservation (reserved + refunded + settled ==
                  initial, always); layer-①④⑤-only configuration reproduces
                  the roadmap's old Phase-3 list and FAILS tests (a)+(b)
                  (red-first proof that ②③ are the load-bearing additions).
Rollback:         revert commit; pure module, imported by nothing yet.
Exit evidence:    commit hash; all four named tests + hammer green; the
                  red-first ①④⑤-only run's failure output preserved.

#### W-K3 RESULT (2026-07-10 — DONE; audit report in docs/plan_audits/)
- Delivered: `include/kalshi/risk_ledger.hpp` (header-only, zero-I/O,
  thread-safe under one mutex — contract #6) + `tests/test_risk_ledger.cpp`
  (the 4 named tests + property battery + red-first, ALL PASS).
- **MONEY IS E6 micro-dollars** (`Micros = int64`), a deliberate deviation
  from the W text's "E4": W-K1 verified live that account money carries six
  decimals, so E4 would be lossy narrowing (D5). Exposure is EXACT from the
  repo fixed-point types: `exposure_micros = CountFp(×10²) · PriceE4(×10⁴)`
  = contracts·dollars·10⁶. No float anywhere (grep-gate on the header).
- Atomicity: reserve() holds one lock across check-AND-deduct of all five
  layers, so the "look then send" race is impossible — the 8-thread × 100
  hammer against a tight total cap admits exactly cap/line, never over.
- Factor derivation: layer ③ key = same underlying / same outcome direction
  (Eggsy: all lines of one event+direction share a key; NHL: one leg's key
  aggregates across combos). EMPTY factor = fail-closed `__unknown_factor__`
  bucket shared by all unclassifiable orders (they throttle together).
- Q8: reduce-risk orders bypass reservation, always admitted, reserve 0.
- ⑤ day-loss is a breaker (realized loss ≥ cap ⇒ refuse new risk; reduce
  still admitted). Conservation invariant `reserved == settled + refunded`
  proven per-handle; settle/refund idempotent-guarded (no double refund).
- client_order_id (contract #9): reservation carries a process-monotonic
  slot; client_order_id(res,...) is stable across retries reusing the slot,
  distinct across slots.
- RED-FIRST preserved: the ①④⑤-only config admits all 10 Eggsy lines (the
  blowup layers ②③ prevent) — proof the additions are load-bearing.
- Consumes: nothing yet (pure module). Feeds the Phase-2 engine's
  reserve-before-send path (S6) and W-K4's day-loss breaker hook.
- Independent audit: ACCEPT-WITH-FINDINGS (named tests genuine + mutation-
  verified; 16-thread mixed hammer conserves). Six fail-OPEN/drift holes on
  the risk core found and ALL FIXED same session (S2 demands fail-closed on
  this module): **N1** copied-handle double-release (a Reservation is
  copyable; the struct-local guard didn't stop an aliased close → used_ went
  NEGATIVE, defeating every cap) — now the LEDGER tracks live slots in an
  open_ set and a close is authorized exactly once; **N2** negative exposure
  admitted → phantom headroom — now rejected as Layer::Invalid; **N3**
  int64 `used+e` could wrap negative past the cap — headroom now checked by
  subtraction (`e > cap - used`, never overflows); **N4**
  __unknown_factor__ inherited the global per_factor default (kNoCap =
  fail-OPEN) — now a dedicated Caps.unknown_factor defaulting to 0
  (fail-closed; operator widens deliberately); **N5** client_order_id was
  ts-arg-dependent (a retry with a different clock changed the id) — ts is
  now captured at reserve, so the id is slot-pure; **N6** factor derivation
  was doc-only — `derive_factor_key(event, long_yes)` now implements the
  canonical rule in code + tests. Regression tests added for each; the
  auditor's four exploit probes (double-refund, negative, overflow, unknown
  default) all re-run fail-closed. Report:
  docs/plan_audits/wK3_audit_2026-07-10.md.

### W-K4 — Rule engine on synthetic scenarios
Purpose:          the always-on defensive rules as a pure library +
                  scenario-tape drills: dead-man expiry (every resting order
                  carries expiry; engine-loss-of-heartbeat ⇒ expiry does the
                  cancelling), cancel-on-disconnect semantics (S6), day-loss
                  circuit breaker (ledger layer ⑤ trip ⇒ quote-stop + panic
                  recommendation), order-rate limiter (write-token budget
                  hook, roadmap 300/s). Scenario tapes: disconnect
                  mid-quote, heartbeat loss, day-loss breach mid-burst,
                  rate-limit saturation. Decisions logged, nothing
                  transmitted (shadow-style).
Allowed writes:   `include/kalshi/dead_man.hpp` (or folded into risk_ledger
                  if the audit prefers one module — recorded either way);
                  `tests/test_rule_engine.cpp`;
                  `tests/fixtures/risk_scenarios/`; Makefile; tools.json.
Forbidden writes: network code; strategy; pipeline.
Acceptance:       each scenario tape's expected decision sequence
                  hand-written and asserted; disconnect tape ⇒ all quotes
                  marked for expiry within the dead-man window; day-loss tape
                  ⇒ breaker trips BETWEEN book updates (Q8 multi-fill window
                  honored); saturation tape ⇒ limiter sheds requotes, NEVER
                  sheds cancels (cancel starvation = classic incident).
Rollback:         revert commit.
Exit evidence:    commit hash; tests green; scenario decision logs.

#### W-K4 RESULT (2026-07-10 — DONE; audit report in docs/plan_audits/)
- Delivered: `include/kalshi/rule_engine.hpp` (a DEDICATED module, not folded
  into risk_ledger — recorded choice: the rules consume the ledger's ⑤ state
  but are their own concern; header-only, zero-I/O, transmits nothing) +
  `tests/test_rule_engine.cpp` (27 checks, ALL PASS).
- Four rules, each a scenario tape with hand-computed expected decisions:
  (1) dead-man expiry — engine heartbeat loss ⇒ Expire ALL resting; a
      per-order expiry_ns expires only the individually-stale one;
  (2) cancel-on-disconnect (S6) — Cancel for EVERY resting order, none
      silently dropped;
  (3) day-loss breaker — wired to the REAL RiskLedger layer-⑤ (the test
      books a loss to the cap and feeds `led.day_loss() >= cap`): a new
      quote intent gets QuoteStop, and a BARE tick (no requote) surfaces
      QuoteStop + a ONE-SHOT PanicRecommend — the breaker trips BETWEEN book
      updates (Q8), and the recommend doesn't spam under a burst;
  (4) rate limiter — new quotes spend the write-token budget (TokenBucketI64,
      roadmap 300/s); saturation SHEDS requotes but **cancels are never
      rate-limited or shed** (cancel-starvation guard, proven by interleaving
      cancels through a saturated burst — all admit).
- Scenario tapes are IN-CODE (recorded scope choice vs the plan's optional
  `tests/fixtures/risk_scenarios/`): the four are compact + deterministic, so
  hand-asserting them in the test is equivalent and self-contained.
- Consumes: RiskLedger (W-K3) day_loss() for the breaker + TokenBucketI64 for
  the limiter. Feeds the Phase-2 engine's always-on defensive layer (S6) and
  informs when to run panic (W-K2).
- Independent audit: ACCEPT-WITH-FINDINGS (four tapes mutation-verified
  genuine; breaker edge-trigger + concurrency probed clean). Two dead-man
  defects found + FIXED: **B1** the engine dead-man armed only after a prior
  heartbeat → an engine that NEVER heartbeats never expired its orders
  (fail-OPEN, the worst case a dead-man must catch) — now adding a resting
  order ARMS the dead-man (add_resting takes a required now_ns); **B2**
  `now - ref` unsigned-underflowed on an out-of-order/skewed tick → spurious
  mass-expiry of the whole book — now guarded with `now > ref` (the same
  guard token_bucket.hpp already had, not carried over). +3 regression tapes
  (dead-from-birth, out-of-order tick, rate-limiter refill — the last closes
  the auditor's D coverage gap); 27→34 checks; both exploit probes re-run
  fail-closed. Report: docs/plan_audits/wK4_audit_2026-07-10.md.

### W-K5 — Reconcile loop (contract #8)
Purpose:          `tools/reconcile.py`: cold-path, periodic — pull exchange
                  resting orders + positions (W-K1) and diff against an
                  engine-state snapshot file; ANY mismatch ⇒ alarm (alert
                  path from W-A5's alert_notify) + report; policy = exchange
                  wins, never blind-retry (S2). In this plan it runs against
                  mock + synthetic snapshots (there is no live engine yet);
                  its contract is what Phase 2's shadow engine must export.
Allowed writes:   `tools/reconcile.py`; `tests/test_reconcile.py` (mock
                  fixtures: ack-loss drift, missed partial fill, disconnect
                  window); tools.json (`offline` mock mode; `network_read`
                  live-view mode).
Forbidden writes: engine state (read-only consumer); pipeline.
Acceptance:       each seeded drift class detected and classified; zero-drift
                  fixture reports CLEAN with counts (a green that can lie is
                  D2-rejected: the report always prints how many orders/
                  positions were compared); exchange-wins policy asserted
                  (the report's recommended action never says "resend").
Rollback:         revert commit.
Exit evidence:    commit hash; tests green; sample drift report.

### W-K6 — LIVE kill-switch rehearsal (**OPERATOR-GATED, S1/S3**)
Purpose:          the S3 rehearsal that unlocks any future live order: with
                  the operator present and confirming per-session (S1), on a
                  funded account: place ONE tiny far-from-touch post-only
                  order (operator-typed confirmation), run panic REAL mode:
                  cancel-all → zero-resting verified via W-K1 → report;
                  repeat once with a deliberately killed network mid-panic
                  (resume/idempotency proof, contracts #8/#9).
Allowed writes:   `docs/RUNBOOK.md` rehearsal section; SESSION_LOG entry;
                  no new code (this W executes what K1–K5 built).
Forbidden writes: everything else. NO strategy orders. NO unattended runs.
Acceptance:       operator-witnessed transcript: order placed → panic →
                  zero resting → report; the interrupted-panic rerun
                  completes idempotently (same client_order_ids on retry).
                  Bootstrap reading (audit N5, recorded for S3's letter):
                  the far-from-touch probe order IS part of the rehearsal —
                  it exists solely to give the kill switch something to
                  kill; no strategy order precedes a completed rehearsal.
Rollback:         panic IS the rollback; account ends flat by construction.
Exit evidence:    transcript + operator sign-off line in SESSION_LOG.
GATES:            S1 stack (all lifecycle gates green) + funding (Phase-3
                  operator item, balance today $0.04) + per-session operator
                  confirmation. This W CANNOT be started by an agent alone;
                  it is scheduled BY the operator.

Ordering: W-K1 → W-K2 → W-K3 → W-K4 → W-K5 → (gate) → W-K6.
K3/K4 may swap if a session prefers; K6 strictly last. One W per fresh
session; independent audit after every W (MASTER_SEQUENCE rule).

---

## 4. Explicitly deferred (recorded so the audit sees intent, not loss)

- OrderSlot pool + `order_json()` allocation-free rewrite, prebuilt template
  store (contracts #2/#3 engineering) → future execution-engine plan
  (DESIGN §6 悬置事项).
- signing p99 / signed-POST RTT p99 measurement → separate task, sampling
  plan to operator first (W-TL1 handoff; DESIGN §5 先测量再优化).
- strategy_seen_ns / book_applied_ns hot-path stamps → STEP 6 execution
  engine plan (BACKLOG).
- PrivateLink / multi-AZ latency bakeoff → post-scale (DESIGN §5).

## 5. Queue position

**RESEQUENCED (operator ruling 2026-07-10, "框架优先,数学后迭代" — see the
MASTER_SEQUENCE amendment): W-K1..K5 are now the FRONT of the queue** —
one W per fresh session, W-K1 → K2 → K3 → K4 → K5, independent audit after
each; then Phase-2 engine/shadow wiring (World A/B merge, its own plan);
pricing W-P2..P4 return afterwards as regressions on the shadow chassis.
W-K6 stays operator-gated AND operator-scheduled (S1 + funding), regardless
of queue order — restated by the operator as non-negotiable, together with
S1–S6, the pessimistic bound, and the shadow-5-green-days gate.

## §6 self-audit (this plan vs GUARDRAILS)

1. Phase/gates (P1, P2): prepares Phase 3 without skipping Phase 2 —
   nothing live until W-K6, which itself requires the full S1 stack. ✅
2. Live orders (S1–S6): W-K6 is the only live-touching W; it is
   operator-scheduled, operator-witnessed, per-session confirmed (S1);
   kill switch standalone (S3); panic live entry = live_order class,
   console-forbidden (S5); reserve-before-send + dead-man + pre-send checks
   are W-K3/K4 admission criteria (S6). ✅
3. Log-odds + fees (Q1, Q3): n/a to the ledger (money in E4); liquidation
   round pricing in W-K2 is crossing arithmetic with fees included in the
   report's cost estimate (Q3 noted in W-K2 acceptance report). ✅
4. Pessimistic bound (Q2): n/a (no strategy claims); W-K6 makes no PnL
   claims. ✅
5. WS trading data (Q5): n/a — no strategy runs here. ✅
6. Tests incl. behavior (E1, Q9): every W ships tests in-change; the four
   named tests are red-first anchored (the ①④⑤-only red proof); Q8
   anti-deadlock is test (c). ✅
7. Pipeline continuity (P4): no W touches capture/ingest/export; panic and
   reconcile are separate processes; alert reuse is additive. ✅
8. Reversible/bounded (P3, P6): dry-run default everywhere; W-K6's rollback
   is the kill switch itself; every W one commit. ✅
9. Docs move with code (E5): RUNBOOK rehearsal section in W-K6; tools.json
   per W; MM_ROADMAP Phase-3 checklist items get their owning W noted at
   execution time. ✅
10. Could a green lie (D2): panic partial-failure exits nonzero with the
    failure list; reconcile CLEAN prints comparison counts; dry-run
    transcripts show every would-be mutation; ledger conservation property
    makes silent leakage visible. ✅
11. **Design-doc completeness (operator requirement for THIS plan):** nine
    contracts folded verbatim (§1) with an owner per contract (binding map);
    five layers ①–⑤ all present in W-K3 with atomic-reserve semantics;
    all four named tests (a)–(d) present, named, and red-first anchored in
    W-K3 acceptance. Deferred contracts (#2/#3 engineering) are explicitly
    tabled in §4, not dropped. ✅

Self-audit verdict: PASS. Independent-audit disposition (2026-07-10,
combined P9 audit — full report in docs/plan_audits/step6_audit_2026-07-10.md):
(i) the W-K2 crossing/post-only reading is constitutionally sanctioned —
GUARDRAILS S3 itself prescribes "reprice-cross liquidation rounds"; the
operator's confirming ruling still gets recorded in a file at W-K2
execution (E2). S6's dead-man clause now covers panic's own liquidation
orders (audit N4, W-K2 acceptance). (ii) W-K6's probe order is defined as
part of the rehearsal (audit N5, W-K6 acceptance). (iii) W-K3 language
choice C++-first (E7 hot-path destiny) — if a later audit judges a Python
reference belongs first, that is a one-line W amendment, not a design
change.
