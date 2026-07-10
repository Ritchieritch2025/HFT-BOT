# SESSION LOG — newest entry first

Every session appends one entry before ending (see CLAUDE.md "Session exit
ritual"). This file is the cross-session memory: where work actually stopped,
which decisions landed in which files, what the next session must know.

---

## 2026-07-10 19:50 UTC — W-P2 DONE ✅ (fair value estimator) — resumed PLAN_PRICING_MODEL Group M after the K-track

- commits: fd08749 (W-P2) + fb40b1f (audit remediation) + this exit. Audit
  verbatim: docs/plan_audits/wP2_audit_2026-07-10.md. NOTE: another session's
  PLAN_RESEARCH_CYCLE_1 commit (f71f60e) interleaved between my W-K5 and W-P2
  commits — no conflict, all work present; its SESSION_LOG entry is below.
- delivered: tools/pricing/fair.py (imports the FROZEN W-P1 lo core — lo.py
  confirmed untouched by the auditor; pure, no I/O) + tests/test_pricing_fair.py
  (14 tests, ALL PASS).
- four components: (a) micro-price computed IN LOG-ODDS space (imbalance-
  weighted average of lo(bid)/lo(ask); balanced 40/60=50c; heavy-ask pulls
  toward bid; one-sided/empty/crossed/off-band/non-finite ⇒ None fail-closed);
  (b) taker-flow drift — bounded lo-shift, coeff/cap = NAMED Group-C
  PLACEHOLDERS (0.10/0.30); (c) bracket-sum — probability-space identity
  (siblings' yes-sum=1), excess redistributed by 1/depth (thin legs move
  most), infeasible dislocation fail-closes; (d) external_anchor_lo NAMED
  slot inert now, blends when weighted (deferred Crypto anchor bolts on later).
- Q9 sign trio (exit evidence): test_q9_drift_sign_and_zero_identity,
  test_q9_bracket_downward_total_excess_thin_moves_most,
  test_q9_bracket_lone_leg_zero_and_consistent_set_zero.
- AUDIT: ACCEPT-WITH-FINDINGS, 2 fixed. B1 (S2 fail-open): NaN/inf qty escaped
  _valid_book because `NaN <= 0` is False → fabricated a nan fair; fixed with
  math.isfinite. D1: a 1/depth bracket redistribution could hand a thin leg a
  correction bigger than its own probability → negative corrected prob → a
  downstream clip silently broke the sum-to-1 identity the constraint exists
  for; bracket_corrections now fail-closes on an infeasible dislocation.
  Auditor confirmed the lo-space micro-price Jensen gap is REAL but exactly
  what Q1 mandates (not a defect). LESSON: `x <= 0` does NOT reject NaN —
  every fail-closed numeric guard needs an explicit isfinite check.
- gates: make check + tests/run_pipeline.sh PASS (61 suites) after
  remediation. fair.py now FROZEN for W-P3.
- blocked / handoff: NEXT agent-executable = W-P3 (A-S quote generator
  tools/pricing/quote.py: reservation_lo = fair − inventory·γ(t), half-width
  δ(t), cap(t), Q6 settlement-window stop, quote-velocity jump breaker, cent
  clamps; 7-name Q9 sign battery; PLAN_PRICING_MODEL §3). Then W-P4 golden
  scenarios. W-P3 consumes fair.py + lo.py (both frozen). Also open: W-K6
  (operator-scheduled live rehearsal); the new PLAN_RESEARCH_CYCLE_1 (below,
  hand-held by the operator); OQ-1 fees; W-A5 24h; env-hardening +
  reconcile-delivery BACKLOG; Phase-2 engine/shadow wiring (needs its plan).

---

## 2026-07-10 — PLAN_RESEARCH_CYCLE_1 landed (operator-approved test plan, hand-held execution)

- commit: this commit (docs/PLAN_RESEARCH_CYCLE_1.md).
- operator decision (E2): all recently identified tests enter one ordered
  plan; operator executes step-by-step with Cowork explaining every number
  ("我需要所有这些测试都进入计划,手把手一步步带着我执行并且讲给我").
- steps: S0 closeout+TL1 deploy → S1 maker-edge pilot dev-grade (spread −
  markout − fees, bounce/drift split, zero-maker-fee series only) →
  S2 signing p99 local benchmark → S3 signed-POST RTT p99 (sampling plan
  operator-gated) → S4 book-level burstiness + category comparison →
  S5 EC2-era rerun (the only go/no-go-eligible version, recv clock) →
  S6 W-K6 (operator-scheduled). Paste-ready S1 quote in appendix A;
  operator checklist ledger at bottom.
- context: plan consolidates Rhys-conversation test items + operator's
  10-point pilot spec (sandbox/research/pilot_maker_edge/); no changes to
  MASTER_SEQUENCE steps; pessimistic-bound and all gates unchanged.
- executed-by: web Claude (Cowork).

## 2026-07-10 18:40 UTC — W-K5 DONE ✅ (reconcile loop, contract #8) — K-track non-live Ws (K1–K5) COMPLETE; only W-K6 live rehearsal remains

- commits: 1a03c2b (W-K5) + 586bd10 (audit remediation) + this exit. Audit
  verbatim: docs/plan_audits/wK5_audit_2026-07-10.md.
- delivered: tools/reconcile.py (cold-path, read-only consumer — mutates no
  engine state, transmits no order) + tests/test_reconcile.py (16 tests).
- SNAPSHOT CONTRACT defined (what Phase-2 shadow engine must export):
  resting_orders[{client_order_id(REQUIRED join key), order_id?, ticker,
  book_side, remaining_count_fp_e4, yes_price_e6?}] + positions[{ticker,
  position_fp_e4}]. Money E6, counts E4, strict-int (no float coercion).
- drift taxonomy, exchange-wins remedies (never resend): ORDER_ONLY_AT_EXCHANGE
  (lost cancel-ack), ORDER_ONLY_IN_ENGINE (missed terminal), ORDER_ATTR_DRIFT
  (remaining/side/price), POSITION_DRIFT / POSITION_DRIFT_LARGE (>=100
  contracts ⇒ hard RUN-PANIC recommend). Exchange source: offline fixture or
  --live via account_view (refuses a field-gate-dropped partial view).
  CLEAN prints comparison counts (D2); malformed/unreadable side ⇒ exit 2
  fail-closed; drift ⇒ alarm line + exit 1.
- AUDIT: ACCEPT-WITH-FINDINGS, 7 findings addressed. THE KEY ONE (N1): orders
  were joined by order_id — which mis-classifies the exact ack-loss case
  reconcile exists to catch (engine keys by client_order_id, hasn't learned
  the exchange order_id yet), splitting ONE live order into a false
  only-at-exchange + only-in-engine pair AND telling the engine a LIVE order
  is terminal. Fixed: join on client_order_id (the deterministic id the
  engine always knows, contract #9). N2 float/bool counts silently coerced →
  false CLEAN → strict-int reject. N3 price drift now compared. N6 large
  delta → hard panic recommend. N4 alarm-delivery gap (nothing forwards
  alerts.log to Telegram) stated honestly + BACKLOG. LESSON: the JOIN KEY
  between two systems is a correctness decision — pick the id BOTH sides
  reliably share (here the one WE generate), not the one one side assigns.
- gates: make check + tests/run_pipeline.sh PASS (60 suites) after
  remediation.
- blocked / handoff: **K-track non-live workstreams (K1–K5) are COMPLETE.**
  The only remaining kill-switch W is **W-K6 — the LIVE rehearsal, which is
  OPERATOR-SCHEDULED + requires funding (S1/S3); an agent cannot start it.**
  So the risk/kill-switch plan has no more agent-executable Ws. NEXT
  agent-executable options for the operator to choose among: resume
  PLAN_PRICING_MODEL Group M (W-P2 fair value → P3 → P4), OR the Phase-2
  engine/shadow wiring (World A/B merge, needs its own plan drafted first),
  OR one of the two env-hardening BACKLOG items (resolve_runtime host / env
  parse_url userinfo) or the reconcile-alarm-delivery BACKLOG item. Standing:
  OQ-1 fees; W-A5 24h; W-K6 awaits the operator.

## 2026-07-10 17:30 UTC — W-K4 DONE ✅ (defensive rule engine) · 4 scenario tapes genuine · audit found 2 dead-man defects, both fixed

- commits: 8be4b9c (W-K4) + 23a4982 (audit remediation) + this exit. Audit
  verbatim: docs/plan_audits/wK4_audit_2026-07-10.md.
- delivered: include/kalshi/rule_engine.hpp (DEDICATED module, not folded into
  risk_ledger — recorded choice; header-only, zero-I/O, transmits nothing,
  shadow-style) + tests/test_rule_engine.cpp (34 checks, ALL PASS).
- four rules, four hand-computed scenario tapes: (1) dead-man expiry
  (heartbeat loss ⇒ Expire ALL resting; per-order TTL expires only the stale
  one); (2) cancel-on-disconnect ⇒ Cancel every resting order (S6); (3)
  day-loss breaker WIRED TO THE REAL RiskLedger ⑤ (book a loss to the cap ⇒
  QuoteStop on new quotes + one-shot PanicRecommend on a bare tick between
  book updates, Q8); (4) rate limiter (TokenBucketI64) SHEDS requotes but
  NEVER cancels (cancel-starvation guard, proven by interleaving cancels
  through a saturated burst). Scenario tapes IN-CODE (recorded scope choice
  vs the optional fixtures dir).
- AUDIT: ACCEPT-WITH-FINDINGS, both dead-man defects fixed: B1 the engine
  dead-man armed ONLY after a prior heartbeat ⇒ an engine that never
  heartbeats never expired its orders (fail-OPEN — the worst case a dead-man
  exists to catch); fixed by arming from add_resting (now takes a required
  now_ns). B2 `now - ref` unsigned-underflowed on an out-of-order/skewed tick
  ⇒ spurious mass-expiry of the whole book; fixed with a `now > ref` guard —
  token_bucket.hpp already had this exact guard but it wasn't carried over.
  +3 regression tapes; both exploit probes re-run fail-closed.
  LESSON: a safety guard proven in one module (here token_bucket's
  non-monotonic-clock guard) must be carried to every sibling that does the
  same unsigned time subtraction — grep for `now.*-.*_ns` on new time math.
- gates: make check + tests/run_pipeline.sh PASS (59 suites) after
  remediation.
- blocked / handoff: NEXT SESSION = W-K5 (reconcile loop, tools/reconcile.py,
  contract #8: pull exchange resting orders + positions via account_view,
  diff against an engine-state snapshot, mismatch ⇒ alarm + report, exchange
  wins never blind-retry S2; mock + synthetic snapshots, no live engine yet).
  After W-K5, the K-track's non-live Ws are complete and W-K6 (live rehearsal)
  is the only remainder — operator-scheduled + funded (S1). Standing: OQ-1
  fees; W-A5 24h; the two env-hardening BACKLOG items.

## 2026-07-10 16:10 UTC — W-K3 DONE ✅ (five-layer reservation ledger, contract #7) · 4 named tests genuine · audit found 6 fail-open holes on the risk core, all fixed

- commits: 5c0f6e5 (W-K3) + 2f06300 (audit remediation) + this exit. Audit
  verbatim: docs/plan_audits/wK3_audit_2026-07-10.md.
- delivered: include/kalshi/risk_ledger.hpp (header-only, zero-I/O, one-mutex
  thread-safe — contract #6) + tests/test_risk_ledger.cpp (39 checks, ALL
  PASS): the 4 NAMED tests (Eggsy 9/10 rejected at factor; NHL leg aggregates
  across combos; deadlock exemption = reduce-risk admitted at cap Q8; refund
  send-fail/timeout/partial restore exactly) + concurrent atomicity hammer +
  RED-FIRST ①④⑤-only proof + the remediation regressions.
- MONEY = E6 micro-dollars (Micros=int64), deliberate deviation from the W's
  "E4" (W-K1 live finding: account money has 6 decimals; E4 = lossy
  narrowing, D5). Exposure EXACT: CountFp(×100)·PriceE4(×10^4) = micro-dollars.
  No float (grep-gate on the header).
- decisions in files: factor key = same underlying/direction, derive_factor_key()
  implements it; unknown_factor = fail-closed 0-default bucket; Q8 reduce-risk
  bypass; ⑤ day-loss breaker; conservation reserved==settled+refunded.
- AUDIT (the pattern held — the risk core got the hardest scrutiny):
  ACCEPT-WITH-FINDINGS with SIX fail-OPEN/drift holes, all fixed same session
  because S2 demands fail-closed here: N1 a COPYABLE Reservation could
  double-release via an aliased copy → used_ went NEGATIVE, defeating every
  cap (fixed: ledger tracks live slots in open_, close authorized once); N2
  negative exposure admitted → phantom headroom (reject Layer::Invalid); N3
  int64 used+e wrap-to-room (headroom by subtraction now); N4
  __unknown_factor__ inherited kNoCap = fail-OPEN (dedicated 0 default now);
  N5 client_order_id ts-arg-dependent broke retry-idempotency (ts captured
  at reserve → slot-pure); N6 factor derivation was doc-only (now coded +
  tested). Auditor's 4 exploit probes all re-run fail-closed.
  LESSON for future sessions: a copyable handle to a resource is a
  double-free waiting to happen — the AUTHORITY for "is this reservation
  still open" must live in the ledger, not the handle struct.
- gates: make check + tests/run_pipeline.sh PASS (58 suites) after
  remediation.
- blocked / handoff: NEXT SESSION = W-K4 (rule engine on synthetic
  scenarios: dead-man expiry, cancel-on-disconnect, day-loss breaker wired
  to ledger ⑤, rate limiter; PLAN_RISK_KILLSWITCH §3). The ledger's
  day_loss breaker + reduce-risk semantics are W-K4's inputs. Standing: OQ-1
  fees; W-A5 24h; W-K5 reconcile; W-K6 live rehearsal (operator-scheduled);
  the two env-hardening BACKLOG items.

## 2026-07-10 14:20 UTC — W-K2 DONE ✅ (panic kill-switch CLI, S3) · operator crossing+dead-man ruling recorded · audit REJECTED×3 on the execute gate then ACCEPT

- commits: 766f943 (W-K2 initial) + remediation commit (d4b4a52) + this
  doc-fix/exit. Audit verbatim (all 3 rounds): docs/plan_audits/wK2_audit_2026-07-10.md.
- OPERATOR RULING (2026-07-10, E2, recorded verbatim in PLAN_RISK_KILLSWITCH
  W-K2): panic 清仓单允许 crossing(吃单)——确定成交优先,taker 费是可
  接受的 panic 成本;每笔 panic 单必须带 dead-man 到期。除 panic 外一切
  策略性退出维持 post-only(费用自杀红线,MM_ROADMAP 1.5B)。
- delivered: apps/panic.cpp — standalone kill switch (no strategy/ring/Redis
  deps; only client/env/wire). Sequence: cancel-all → verify-zero-resting →
  reprice-cross IOC+reduce_only liquidation rounds → machine-parseable
  report. Dead-man IMPLEMENTED as time_in_force=immediate_or_cancel (an IOC
  can never orphan as a resting order — strongest dead-man) + reduce_only
  (can shrink risk, never create it, Q8). Dry-run is DEFAULT (plan printed,
  journal-proven silent). --execute gated: localhost-loopback drill (tests)
  OR require_orders_allowed (S1 live arming). panic_live = live_order class,
  console-forbidden (proven). + tests/mock_exchange_panic.py +
  tests/test_panic_dryrun.py (10 drills).
- client_order_id (contract #9): wire::client_order_id from (ts_ns,
  strategy_id=29, seq) — run-stable, byte-identical on retry; ack-loss
  drills prove zero double fills. ASSUMPTION (W-K6 live-confirm): exchange
  dedupes orders by coid + answers 409 (openapi documents this only for
  transfers) — reduce_only + re-enumeration bound the worst case.
- context capsule: THE HARD PART was the execute gate. Audit rejected it
  THREE times, each closing one more URL-authority bypass under
  KALSHI_HOST_UNSAFE_OVERRIDE=1 (a dev flag that skips the host allowlist,
  refused only in LIVE mode — panic's drill path runs non-live, so that
  guard doesn't engage): (1) keyed on env LABEL only →
  KALSHI_BASE_URL=prod-host fired unarmed; (2) substring find("://localhost")
  → localhost.evil.com + 127.0.0.1@evil.com (userinfo); (3) authority split
  at '/' only → external-api.kalshi.com?@127.0.0.1 (query terminator — curl
  ends authority at '?', we read the query's 127.0.0.1 as host). FINAL fix:
  base_host_is_loopback is a COMPLETE RFC-3986 authority parse — terminate
  at first '/?#', strip userinfo at last '@', strip port, [::1] brackets,
  case-fold — regression-tested against all 8 vectors; auditor cleared 18
  corners + libcurl connect-target checks. DEAD END for future sessions: do
  NOT re-derive a connect host with substring or partial parsing anywhere
  near a safety gate. BACKLOG has the deeper fix (resolve_runtime should
  expose ONE validated host) + the env.cpp parse_url userinfo quirk.
- live demo (read-only dry-run): orders=0, positions=0 — account genuinely
  flat (the W-K1-era MVE position had since settled/closed, re-verified via
  raw GET). Confirms simdjson handles the real API's alphabetical field order.
- gates: make check + tests/run_pipeline.sh PASS (57 suites) after the
  round-3 fix.
- blocked / handoff: NEXT SESSION = W-K3 (five-layer reservation ledger,
  PLAN_RISK_KILLSWITCH §3 — the four named tests Eggsy/NHL/deadlock/refund +
  red-first ①④⑤-only proof). W-K3 MONEY must be E6 micro-dollars (or state a
  narrowing rule) — the W-K1 live finding that account money carries 6
  decimals. Standing: OQ-1 fees; W-A5 24h items; the two BACKLOG env-hardening
  items; W-K6 will live-confirm the 409-coid assumption.

## 2026-07-10 11:30 UTC — W-K1 DONE ✅ (typed read-only account endpoints) · live acceptance on the REAL account · audit initially REJECTED (2 blocking), remediated same session to green

- commits: 342b798 (W-K1 + live acceptance) + remediation/exit commit (this
  one). Audit verbatim: docs/plan_audits/wK1_audit_2026-07-10.md.
- delivered: `tools/account_view.py` — GET balance / positions / resting
  orders; openssl RSA-PSS signing mirroring src/client.cpp (MGF1=SHA-256
  default proven empirically by the auditor); D3 field gates with counted
  drops; `--assert-zero-resting` panic primitive (W-K2's building block);
  redirects REFUSED (signed headers never forwarded); read-only by
  construction (GET only, grep-gated). 17 tests vs a local mock incl. PSS
  round-trip through openssl verify + credential redaction.
- FIELD-SEMANTICS FACTS (VERIFIED-LIVE 2026-07-10, now in the W-K1 RESULT
  block): ① position money carries SIX decimals with nonzero sub-centicent
  digits (real value "4.726960") ⇒ account money is parsed to E6
  micro-dollars, byte-exact, floats rejected — E4 would be lossy narrowing
  (D5); recorded DEVIATION from the W text ("E4 money"), evidence-forced.
  ② `balance` (cents int) sits within 1 cent of `balance_dollars`
  (fixed-point, authoritative) — consistent with floor, ONE sample, round
  not excluded; cross-check accepts <1 cent, fails closed at >=1 cent.
  ③ GET /portfolio/orders defaults to ALL subaccounts (zero-resting is
  account-wide ✓) but /portfolio/positions defaults to PRIMARY-ONLY —
  W-K5 reconcile flag. ④ W-K3 ledger must adopt E6 money or state its
  narrowing rule (flagged in RESULT).
- AUDIT (the process worked): initial verdict REJECT — B1: pagination
  truncation at MAX_PAGES printed WARN but still reported "ZERO — clear"
  rc=0 (fail-OPEN in the panic primitive; auditor proved it with an
  endless-cursor mock); B2: unicode-aware \\d regex accepted Arabic-Indic
  digits and COMPUTED WRONG MONEY (parse_e6('4.72696٠')→4728544). Both
  fixed (truncation now raises everywhere; ASCII [0-9] like gold_load) +
  4 new tests (truncation⇒rc1, unicode reject e2e, floor-vs-round
  discriminating fixtures, redirect refusal). 13→17 tests.
- live acceptance (operator's real account): balance $23.261400 (NOTE:
  account was FUNDED — memory said $0.04; Phase-4 funding gate item is
  moving), portfolio value $4.70, 3/3 positions parsed 0 dropped —
  including a KXMVECROSSCATEGORY combo position (25.69 contracts,
  $4.726960 exposure; MVE = Q7-excluded from MM candidacy, noted as an
  operator-held position, no action), resting orders 0,
  --assert-zero-resting rc=0.
- incident (not mine, surfaced + resolved): found src/storage.cpp in the
  working tree with an uncommitted single-character change DELETING a
  semicolon from committed code (pure syntax breakage, broke ws_shadow_mock
  compile mid-gates; plausibly an editor miskey from a human/browsing
  session). Restored to HEAD via git checkout; pipeline re-ran green. No
  other stray modifications found at exit.
- gates: make check + tests/run_pipeline.sh PASS (56 suites) before AND
  after remediation.
- blocked / handoff: NEXT SESSION = W-K2 (panic CLI dry-run,
  PLAN_RISK_KILLSWITCH §3; account_view is its verify primitive — consume
  --assert-zero-resting rc, remember rc=1 also means UNPROVABLE/truncated).
  W-K2 must record the crossing/post-only operator ruling per E2 at
  execution. Standing: OQ-1 fees; W-A5 24h items; Group C gates.

## 2026-07-10 08:55 UTC — W-P1 DONE ✅ (log-odds core) + riders ③: reverse-scan caught 4 real drifts · audit ACCEPT-WITH-FINDINGS (0 blocking), all applied

- commits: W-P1 commit (94f9d74 per audit) + audit-fix commit (this exit).
  Full audit verbatim: docs/plan_audits/wP1_audit_2026-07-10.md.
- delivered: `tools/pricing/{__init__.py,lo.py}` (logit/expit, E4↔lo maps,
  conservative grid quantization — bid floors / ask ceils; fee plumbing
  DELEGATED to mm_research.trade_fee, maker-rate lookup fail-closed on
  flat/unknown fee_type; gate-before-enum locked by test) +
  `tests/test_pricing_lo.py` (15 tests after audit) + W-P1 RESULT block in
  PLAN_PRICING_MODEL.
- riders (operator ruling ③): check_registry REVERSE SCAN (disk→registry;
  E3 machine-checked; scope + laundering residual documented in docstring).
  RED-FIRST evidence: first run flagged 4 real unregistered scripts —
  rtt_baseline_sampler.sh, rotate_metrics.sh, dashboard_net.sh, load_db.py
  — all four now registered with verified safety classes (network_read /
  offline / network_read / offline), autorun:false.
- golden table (exit evidence, hand-derivable): lo(1c)=−4.59512,
  2c→1c=+0.70330 vs 50c→49c=+0.04001, edge/mid asymmetry 17.6×; fees C=1
  {1c,50c,99c}→{$0.0007,$0.0175,$0.0007} (ceil_to_centicent per
  kalshi_facts.yaml; repo yaml still fees.verified:false so gate-mode
  refuses BY DESIGN, OQ-1 pending).
- context capsule: (1) fee formula/rounding has exactly ONE implementation
  (mm_research.trade_fee); lo.fee_raw duplicates only the pre-rounding
  polynomial for Q9 limit tests, consistency locked by test (audit N4).
  (2) NORMATIVE port notes in lo.py docstring: side=None ties round
  half-up; _EPS=1e-9 grid-snap is spec (worst-case tighten ≈ $1e-11);
  Python fees are float dollars — C++ port lands fees on integer centicent
  (audit N5). (3) TRIPWIRE: when OQ-1 flips fees.verified to true, one
  test in test_pricing_lo.py intentionally fails — update it consciously
  (audit N6, also flagged in the PLAN RESULT block). (4) reverse-scan
  residuals: args_template laundering possible (defends accidental drift,
  not adversarial edits — that's the audit's job); globs non-recursive by
  design (package modules/tests/apps covered elsewhere). (5) audit
  independently torture-tested quantization: 99 cents + 989 deci-cents +
  centicents at ±1 ulp, zero on-grid moves.
- gates: make check + tests/run_pipeline.sh PASS (55 suites) before AND
  after audit fixes.
- RESEQUENCING RULING (operator, 2026-07-10, after W-P1 exit — verbatim in
  the MASTER_SEQUENCE amendment): 框架优先,数学后迭代 — W-P2..P4 yield;
  K1..K5 first, then Phase-2 engine/shadow wiring (World A/B merge per
  ARCHITECTURE_REVIEW + PLAN_LIVE_VALIDATION, interfaces per
  DESIGN_HOTPATH §4 + the three terms mapped in the amendment: same-flow
  three modes / pluggable strategies / zero-allocation acceptance);
  P2..P4 return as regressions on the shadow chassis; Group C waits for
  recv data as before. NON-NEGOTIABLE restated: S1–S6, pessimistic bound,
  shadow 5 green days, W-K6 + all live actions operator-scheduled.
- blocked / handoff: **NEXT SESSION = W-K1** (typed read-only account
  endpoints, PLAN_RISK_KILLSWITCH §3). W-P1 froze tools/pricing/lo.py
  (defects go back through a filed note; W-P2 deferred per ruling).
  Standing: OQ-1 fee ratification (operator); W-A5 24h items; Group C
  gates unchanged.

## 2026-07-10 07:10 UTC — STEP 6 paper W DONE ✅ (P9): PLAN_PRICING_MODEL + PLAN_RISK_KILLSWITCH drafted · DESIGN_HOTPATH folded byte-verbatim · combined audit ACCEPT-WITH-FINDINGS, all applied

- commits: plan drafts + audit-fix commit (B1+N1..N6,N8 applied; audit
  verbatim in docs/plan_audits/step6_audit_2026-07-10.md). Paper-only: no
  code executed, no config changed, no EC2/S3 touched (audit check H
  confirmed the plan commit touches only the two docs).
- session note: found the working tree parked on `main` at 92918bb (reflog:
  a prior session checked out main after committing 331f816/6098584/6da7317
  on the work branch); switched back to plan-live-validation-p0-p3 — no
  tracked changes were lost (verified clean status before checkout).
  ATTRIBUTION CORRECTED (operator, 2026-07-10): the main-checkout was most
  likely the live-readiness audit session (its report self-describes
  checking out main), not the DESIGN-doc session. No fault assigned.
- decisions (each in its file):
  - Phase 1.5 decomposition → docs/PLAN_PRICING_MODEL.md: Group M math-now
    (W-P1 lo-core → W-P2 fair value → W-P3 A-S generator → W-P4 golden
    scenarios, all synthetic, Q9 batteries named per W) vs Group C
    calibration (W-C1 λ(δ) → W-C2 toxicity → W-C3 vol/jump → W-C4 go/no-go),
    gated on 7 clean days (2026-07-13).
  - **Recv-clock law: Group C is clock=recv EXCLUSIVE for every W** (audit
    B1 closed the draft's shape-only carve-out; it survives only as an
    OPTION requiring an operator ruling recorded per E2). Honesty note in
    the gate box: ladder-era data makes go/no-go realistic ≥ ~2026-07-17;
    2026-07-13 is the clean-days gate only.
  - Risk/kill-switch decomposition → docs/PLAN_RISK_KILLSWITCH.md: W-K1
    typed read-only endpoints → W-K2 panic CLI dry-run (live entry =
    live_order, console-forbidden) → W-K3 five-layer reservation ledger
    (C++, E4) with the four NAMED tests (Eggsy replay / NHL template /
    deadlock exemption / refund exact-restore) + red-first ①④⑤-only proof →
    W-K4 rule-engine synthetic drills → W-K5 reconcile loop → W-K6 live
    rehearsal (OPERATOR-GATED S1/S3, operator-scheduled only).
  - DESIGN_HOTPATH §4–5 folded byte-verbatim (mechanical diff = identical,
    re-verified after audit edits); every one of the nine contracts has an
    owner in the binding map; deferred items (#2/#3 OrderSlot/templates,
    p99 measurement, PrivateLink) recorded as deferred, not lost.
- context capsule: seven-field W format = Purpose/Allowed writes/Forbidden
  writes/Acceptance/Rollback/Exit evidence (+gates where relevant; Allowed
  reads dropped per P7). Key audit-verified facts baked into the plans:
  mm_calibrate already emits vol_1min_lo/tox_120s_lo; kalshi_facts.yaml fee
  regime = ceil_to_centicent with fees.verified:false (OQ-1 pending ⇒
  gate-mode fee math fail-closes by design); FIRST_CLEAN_DAY=2026-07-06 in
  gate_calc; W-P1 golden lo values 2c→1c≈−0.70 vs 50c→49c≈−0.04; BADAMS
  zero-trade 37¢→28¢ jump is the breaker's regression anchor (breaker
  watches quote velocity, never trade volume). Dead end closed: don't let
  ANY calibration input use exchange-clock data "because it's just shape" —
  λ(δ) parameters launder look-ahead into a formally-clean recv backtest
  (audit B1's core argument).
- OPERATOR RULINGS (2026-07-10, same session — all landed in files):
  ① sequencing APPROVED: Group M starts ahead, one W per session,
  W-P1→P2→P3→P4; W-K1..K5 interleave after; W-K6 operator-scheduled
  (→ MASTER_SEQUENCE amendment + both plans' §5).
  ② pre-ladder shape-only option REFUSED: calibration recv-clock only, no
  exceptions; exploratory shape analysis sandbox/-only (P8), outputs never
  enter calibration params or go/no-go (→ PLAN_PRICING_MODEL §4 gate box).
  ③ rtt_baseline_sampler.sh KEPT — next code session registers it in
  tools.json; check_registry reverse-scan gap → BACKLOG (both in the
  2026-07-10 BACKLOG entry).
- blocked / handoff: **NEXT SESSION = W-P1** (log-odds core,
  PLAN_PRICING_MODEL §3) — plus the two riders from ruling ③ (register
  rtt_baseline_sampler.sh with a verified safety class; reverse-scan check).
  W-K2 crossing ruling still gets recorded per E2 at W-K2 execution time.
  Standing: W-A5 24h-gate items (see the W-A5 entry below); Group C gates
  unchanged (7 clean days + ladder-era recv data + OQ-1 fees).

## 2026-07-10 — Hot-path execution design notes archived (operator-directed)

- commit: this commit (docs/DESIGN_HOTPATH_EXECUTION_2026-07-10.md).
- operator decision (E2): archive the hot-path order-engine discussion as
  design-input notes ("先起一个文档把我们的思路加进去"). NOT a plan, no W.
- content: current-stack honest positioning (capture/vault/watch, not yet
  low-latency execution); "pointer" intent translated to memory-resident
  order engine; repo skeleton references (wire.hpp/ring.hpp/tradingd.cpp/
  client.hpp Lanes); nine-point hot-path contract (operator's six: in-memory
  state, prebuilt templates, OrderSlot pool, warm lanes, per-order RSA-PSS,
  no Redis on hot path + three additions: reserve-before-send quota,
  cold-path reconcile loop, client_order_id idempotency); sequencing
  discipline (safety before speed, measure signing/RTT p99 before
  micro-optimizing, first-edge-not-speed per interview archive).
- handoff: STEP 6 plan-drafting sessions MUST fold this doc verbatim into
  PLAN_PRICING_MODEL / PLAN_RISK_KILLSWITCH / execution-engine plan.
- executed-by: web Claude (Cowork).

## 2026-07-10 04:40 UTC — W-TL1 DONE ✅ (timestamp ladder, local-only): 4 ladder columns on all 3 fact tables · backtest default recv clock + look-ahead demo · jitter tool · audit ACCEPT-WITH-FINDINGS, all applied

- commits: b004ec7 (carry-forward: PRIOR session's uncommitted console/registry
  work found in the tree — dashboard broken-pipe quiet handler, SSE test,
  lifecycle primary stages, tools.json autorun/cwd flags + run_tests '<'
  filter; verified green before committing, kept separate from W-TL1 for
  audit provenance) · 7495173 (W-TL1 implementation) · 57a7734 (audit
  findings applied).
- decisions (each in its file):
  - Ladder schema + heartbeat contract + ts_utc=legacy-COALESCE →
    tools/ingest.py (DDL comment) + docs/warehouse_schema.md "Timestamp
    ladder" section.
  - Backtest clock policy (recv default, fail-closed exit 3, exchange =
    diagnostic-only) + minimal active-quote latency model + N3/N4 honesty
    bounds → tools/mm_backtest.py docstring.
  - 3 p99 latency params = conservative PLACEHOLDERS (2000/5000/60000 µs) →
    config/backtest_latency.yaml; real measurement = separate task, sampling
    plan to operator first.
  - Operator spec preserved VERBATIM → docs/plan_audits/wTL1_spec_2026-07-10.md;
    audit report VERBATIM → docs/plan_audits/wTL1_audit_2026-07-10.md.
  - BACKLOG timestamp-ladder entry annotated with landed/still-open split →
    docs/BACKLOG.md.
- context capsule: (1) exchange_ts_us: ts_ms authoritative ×1000; legacy ts
  fallback number=SECONDS, string=ISO-8601, else NULL, all plausibility-
  windowed (TS_MIN/MAX_US); recv_wall_ns/recv_mono_ns raw envelope values,
  local_recv_ts_us = wall//1000; implausible wall nulls both. (2) heartbeats:
  ts_utc = hour start ALWAYS + all-NULL ladder ALWAYS; consumer-side
  heartbeat predicate needs the hour-alignment guard (ts_utc % 3600e6 == 0)
  or legacy price-less first-observations leak past fail-closed (audit N2 —
  probed 0 such rows in 47.2M staging + 50.3M archive rows, guard added
  anyway + negative test). (3) look-ahead demo numbers: late-arrival fixture
  (book exch T recv T+500ms; trades through the touch at T+100/200ms):
  clock=exchange pessimistic fills=2 pnl=+$0.10; clock=recv+not-before
  fills=0 (tests/test_backtest_clock.py, runs in pipeline). (4) csv.gz dtype
  trap: an all-NULL ladder column sniffs VARCHAR and UNION BY NAME would
  drag BIGINT→VARCHAR; load() introspects (binding-only) and TRY_CASTs
  present ladder cols back to BIGINT (tools/warehouse.py); read_csv types=
  errors on absent column names, hence introspection. (5) .gitignore: work/
  → work/* + !work/research/ + work/research/* + !work/research/jitter_report.py
  (git cannot re-include under an ignored parent dir); audit verified no
  data became trackable; nested foo/work/ would no longer auto-ignore (N6).
  (6) audit measured the additive migration on a COPY of the real 2.6GB/47.2M-row
  staging: 12 ALTERs in 0.963s ⇒ EC2 restart cost negligible (P4). (7) Dead
  end ruled out: read_csv types={} cannot pre-pin columns that are absent
  from old csv files — binder error, don't retry that route. (8) jitter tool
  residual chains break on file/stream_epoch/missing-field boundaries;
  out-of-order Δexchange<0 counted never differenced; capture_host is a
  REQUIRED declared-provenance flag (ec2|mac-precutover|unknown_overlap|
  fixture), only ec2 feeds go/no-go; ms-granularity footnote is embedded in
  CSV header comments + stdout.
- production note (P4): EC2 pipeline untouched this session (Forbidden list
  honored: no EC2, no S3, no archive rewrite, no backfill). The box adopts
  the ingest changes at its next deploy+restart — which is already scheduled
  as tomorrow's deliberate graceful-stop test (W-A5 handoff); migration cost
  measured negligible, capture continuity preserved (additive ALTER on init,
  same loop).
- blocked / handoff: ① W-A5 24h-gate session items still pending (capture_gaps
  verdict, pmset, Telegram token, CloudWatch alarm, AWS budget — see the
  2026-07-10 W-A5 entry below, unchanged). ② W-TL2 (historical backfill:
  rebuild-from-raw facts_v2 + row-count/key-level diff; never join-patch)
  opens only after operator reviews W-TL1. ③ latency measurement task:
  sampling plan → operator BEFORE running; signed-POST RTT p99, never GET
  means. ④ production EC2 jitter report = after deploy. ⑤ NOTE: untracked
  strays in the tree not mine to adjudicate: tools/rtt_baseline_sampler.sh
  (unregistered tool in tools/, E3 drift if kept), sandbox/discovery/*,
  sandbox/research/, 盒子操作卡_BOX_CLI.md — sandbox items are P8-exempt;
  the tools/ stray needs an owner ruling.

## 2026-07-10 — W-A5 machinery DONE ✅ (24h gate + operator items open): delta vaulted, sync timers live, alerting built, riders a/b/c live, full-L1 = 11,307/11,307

- commits: 415ac7e (batch 1: riders+SIGTERM+caps+sync+alerting+report-pull+
  facts) · policy-test fix · report-pull two-stage · TimeoutStopSec=90 ·
  sample_size=-1 regression fix · this exit commit. Evidence:
  docs/plan_audits/wA5_steady_state_2026-07-10.md (no-fabrication table).
- ①-⑨ status: ① Mac delta (17:00→23:09 residual) 49 files 3-hop 0-mismatch
  → vault now holds the COMPLETE Mac era. ② hourly(:05)+daily(03:10Z)
  EC2→S3 timers enabled, first hourly run proven (00:05:16Z journalctl).
  ③ alert_notify (capture/freshness/disk → Telegram-or-log, state-change
  anti-spam, 5 dry-run tests) every 60s; report flow-back PROVEN onto the
  Desktop (TCC two-stage fallback). ④ run-rate ~$110/mo + S3 raw accrual
  ~+$20/mo/month; bill+$150-budget = operator. ⑤ sleep re-enable deferred
  to the 24h verdict (by plan). ⑥ (a) rotate_metrics keep-3 wired+tested;
  (b) ALL categories class A — verified end-to-end: series_classified
  11,307/11,307 record_class='A'; (c) CSVs untracked+ignored. ⑦ staging
  92GB/1,132 files deleted (operator-approved; disk 65%→19%).
  ⑧ ticker_conflation → kalshi_facts (VERIFIED-MEASURED). ⑨ SIGTERM: trap
  split + backgrounded ws_shadow (supervisor exits <1s, contract-tested) +
  TimeoutStopSec=90 for slow children (ws_shadow drain, python-in-DuckDB);
  PARTIAL by honest definition — children may still hit the 90s backstop;
  avoid restarts 00:00-00:15Z. Events cap: BOTH events AND markets had
  silently truncated at 80k EVERY hourly crawl (the "(capped)" marker
  screamed into an unread log — D2 lesson); raised to 2000 pages; markets
  now complete (~50k); events endpoint is ALL-HISTORY (hit 400k cap too;
  newest-first pagination so only historical tail trimmed; redesign =
  BACKLOG with E4 spec-check). The raise exposed a latent schema-sampling
  crash (mixed timestamp precisions at row 363,172) — fixed same session
  (sample_size=-1, VARCHAR fallback, regression test).
- incidents this session: 00:09:48Z restart landed mid-export → 4 children
  SIGKILLed at +30s (supervisor itself exited clean — fix working); export
  self-healed via write-once + 02:00 force sweep, as designed. Box git pull
  blocked once by churned CSVs (rider (c)'s raison d'être) — resolved.
  Stale Mac git HEAD.lock cleared (concurrent research session). battery:
  run_pipeline PIPELINE PASS both boxes at final code.
- INDEPENDENT AUDIT (hardest one yet, and it earned its keep): verdict =
  machinery sound for overnight, BUT 3 blocking-class findings, all applied:
  **B1 rider (a) was DEAD in production** (invocation redirected into the
  root-owned supervisor.out.log; ubuntu-uid open failed; `|| true` swallowed
  it; metrics hit 5.4GB with zero rotations) — redirect dropped, LIVE
  verification = .1 file at next hour boundary; **B2 my "supervisor exited
  clean" evidence was FALSE and is RETRACTED** (journal shows killed pid
  81442 WAS the supervisor main, and it ran pre-fix code — the graceful-stop
  fix is static-tested only; first real stop is the live test); **B3 disk
  fuse ~2-4 days** (raw 28-32GB/day measured + the B1 metrics leak) — B1
  fixed; retention-vs-EBS ruling handed to operator. Also applied: N1 ingest
  bounced (loads classes once at init; waiting out the research-chain
  staging lock, N4 self-heal); N2 catalog outage window stated plainly
  (23:57Z deploy -> 01:00Z first clean supervised cycle); N5 RUNBOOK §6 ops
  surface; N7 newest-first marked ASSUMPTION. **N6 SECURITY (operator!):
  ~/.bash_history on the box holds plaintext AWS key export lines from the
  W-A2/A3 era — rotate the vaultWriter IAM key + clear history.** Auditor
  independently verified: vault delta ETag spot-checks byte-exact; mac-vault
  1,132 objects intact post-deletion; current-hour glob exclusion PROVEN
  correct (no date=2026-07-10 prefix during hour 00); alert timer 1h+ zero
  errors; hour boundary rolled cleanly under the new loop.
- OPERATOR RULING (2026-07-10, post-audit): **RAW_RETENTION_DAYS 3 -> 2**
  (raw vaulted to S3 hourly since W-A5; audit B3 disk math). Changed in
  supervisor default + config/warehouse.yaml + warehouse_common fallback.
  TAKES EFFECT at the next supervisor restart — scheduled for tomorrow's
  24h-check session as a DELIBERATE restart that doubles as the pending
  graceful-stop LIVE test (avoid 00:00-00:15Z; expect no SIGKILL in
  journalctl, that is the pass criterion).
- blocked / handoff: NEXT SESSION (short, after 2026-07-10 23:09Z):
  ① capture_gaps --date 2026-07-10 on the box + the 00:10→00:10 window ⇒
  24h zero-gap verdict; ② if green: operator runs `sudo pmset -a
  disablesleep 0` (Mac sleeps again) ⇒ **STEP 1 CLOSED** ⇒ update
  MASTER_SEQUENCE amendment + memory. OPERATOR standing items: Telegram
  token+chat_id into box env.sh (then ping me to test-fire); CloudWatch
  StatusCheckFailed→SNS→email; bill figure + $150 AWS Budget; TCC grant if
  tomorrow's 09:00 pull WARNs. BACKLOG added: events crawl redesign;
  3e-style L1 backfill from vaulted raw; rider (d) auto-deploy; S3
  Glacier/retention ruling.

## 2026-07-10 ~03:00 UTC — OPERATOR RETRACTION: direction proposal + audit system DELETED; interview research kept as strategy-cornerstone archive

- operator decisions (E2, this entry is the record):
  (1) PROPOSAL_DIRECTION_ADJUST + its audit + AUDIT_PROTOCOL.md + AGENTS.md
      — all DELETED ("文件都先删掉 我们还没敲定呢 不要随便加").
      Nothing from them is in force. Recoverable from git history if ever
      needed (P6), but treat as void.
  (2) No complex audit system; the pre-existing workflow (CLAUDE.md +
      GUARDRAILS, independent audit as practiced) stands unchanged.
  (3) The podcast-derived research IS archived as strategy cornerstone:
      five RESEARCH_INTERVIEW_*_2026-07-09.md analyses +
      RESEARCH_EDGE_HYPOTHESES_2026-07-09.md (H1-H13, hypotheses only,
      not plans). Direction statements (§4.5 sports-first / no-ML) were
      REMOVED from the hypotheses doc — direction is NOT decided.
- state: MM_ROADMAP / MASTER_SEQUENCE never modified by any of this;
  current queue unchanged (next per MASTER_SEQUENCE + 2026-07-13
  seven-clean-days gate).
- blocked / handoff: none. Next session: proceed per MASTER_SEQUENCE;
  read the research archive for context, execute nothing from it without
  operator instruction.

## 2026-07-10 ~02:00 UTC — Cross-agent audit system established (AGENTS.md + AUDIT_PROTOCOL); multi-agent plan-review circuit queued

- commits: this commit (AGENTS.md, docs/AUDIT_PROTOCOL.md, proposal
  appendix D, this entry).
- operator decisions (E2): (1) approved creating AGENTS.md + AUDIT_PROTOCOL
  ("动吧"); (2) the direction/testing plan must be reviewed by EVERY agent
  surface multiple passes before execution ("每个都过几遍 把计划细化").
- deliverables:
  - AGENTS.md (repo root): binds Codex/any agent to CLAUDE.md + GUARDRAILS
    — closes the gap where Codex operated constitution-blind (it reads
    AGENTS.md, not CLAUDE.md).
  - docs/AUDIT_PROTOCOL.md: executor≠auditor, cross-vendor preferred;
    auditors RUN acceptance (A1); auditors never fix (A2); mutation
    spot-check for test authenticity (A4); auditor-built independent
    fixtures (A5); dual audit for strategy/risk/order-path Ws (A6);
    executed-by/audited-by recorded in SESSION_LOG (A8); plan-review
    circuit R1 ClaudeCode feasibility → R2 Codex adversarial → R3 Cowork
    consolidation → operator approval (§3).
  - Proposal appendix D: paste-ready R1/R2 prompts for the operator;
    round cap (1 circuit + optional recheck) to prevent review churn.
- next: operator pastes R1 prompt to Claude Code, R2 to Codex, returns
  both reviews here for R3 consolidation into proposal v1.2.
- executed-by: web Claude (Cowork) · audited-by: pending (this is paper;
  R1/R2 of the circuit double as its audit).

## 2026-07-10 ~01:30 UTC — Proposal audited (9 findings, 2 HIGH) and fixed to v1.1; now agent-executable

- commits: this commit (audit doc + proposal v1.1 appendices + this entry).
- audit: docs/plan_audits/audit_PROPOSAL_DIRECTION_ADJUST_2026-07-10.md.
  Verdict on v1.0: direction/skeleton sound, no MUST violations, but NOT
  directly executable. HIGH: (F1) data-plane stale — post-cutover EC2 is
  sole data owner, Mac archive has only 07-06..08, W-S1 must run on EC2
  (16GB ⇒ chunked), 7-day gate 2026-07-13 ⇒ INTERIM vs final run;
  (F2) W-S1 lacked seven-field task card. MED: (F3) W-S2+ needs
  MASTER_SEQUENCE amendment mechanism; (F4) handoff quote violated
  one-W-per-session — split into session A (paper, P9) + session B (W-S1);
  (F5) Q7 conflict registered — RFQ combos are MVE-class, Q7 excludes them
  from MM candidacy; future RFQ requires formal Q7 revision (operator-
  approved constitution change). LOW: F6 W-S2 eval criteria, F7 World Cup
  sample skew (also an upper-bound rec-flow opportunity), F8 rollback
  story, F9 affected-docs list. All 9 fixed in proposal v1.1 (Appendix A
  seven-field W-S1 card with metric definitions in log-odds + acceptance
  as runnable demo; Appendix B corrected two-stage handoff quotes;
  Appendix C feed evaluation template).
- evidence gathered: GUARDRAILS read in full; MASTER_SEQUENCE read;
  mm_scan/mm_backtest/mm_calibrate/mm_research confirmed present in
  tools/; warehouse partitioning confirmed (category/subcategory columns,
  taker_side in trades; Sports has 21 subcategories capturing);
  seven-field template taken from PLAN_EVENT_PACKAGING W-E0..E4.
- blocked / handoff: awaiting operator approval of v1.1 → then session A
  (paper plan-change W) per Appendix B quote.

## 2026-07-10 ~00:30 UTC — OPERATOR DIRECTION SHIFT: sports-first, infra-over-models; adjustment proposal written

- commits: 4ba29ff (sports direction, E2) · e13d51c (no complex predictive
  models; feed+mapping+infra focus, E2) · this commit
  (PROPOSAL_DIRECTION_ADJUST_2026-07-09.md + this entry).
- operator decisions (E2, all in RESEARCH_EDGE_HYPOTHESES §4.5 +
  PROPOSAL_DIRECTION_ADJUST):
  (1) sports is the target market ("最赚钱的市场还是在体育");
  (2) no complex predictive-model training — engineering focus =
      realtime feed + mapping layer + technical details;
  (3) crypto-first plan lacked market-participant feedback; adjust
      direction per 5-interview intel and iterate fast.
- deliverable: docs/PROPOSAL_DIRECTION_ADJUST_2026-07-09.md — proposal
  (NOT yet in force): keep pipeline/AWS/gates unchanged; swap 1.5A anchor
  (spot index → sportsbook odds feed, buy-vs-scrape = operator money
  decision at W-S2); rework 1.5 effort (cut predictive modeling, add
  mapping layer + quote-funnel logging + defensive calibration);
  fast-iteration loop W-S1..W-S4 with all pessimistic-bound gates intact.
  W-S1 = category-comparative scan on EXISTING warehouse data (sports vs
  crypto: spread/depth/arrival/toxicity/rec-features), zero procurement.
- blocked / handoff: awaiting operator approval of proposal → next fresh
  session executes the formal plan-change W (MM_ROADMAP revision + audit)
  then W-S1. MASTER_SEQUENCE untouched meanwhile; STEP 1 AWS migration
  needed under either direction.

## 2026-07-09 ~23:30 UTC — Interview intel #4 (TroyCuban RFQ) + #5 (World Cup risk) committed c088965; hypotheses now H1-H13

- commits: c088965 (analysis docs #4 T=TroyCuban, #5 W=WorldCup, hypotheses
  update, 2 vendor transcripts). Series complete: 5 interviews analyzed,
  all mapped into RESEARCH_EDGE_HYPOTHESES_2026-07-09.md.
- key adds: H13 anchor-manipulability check (verify Kalshi crypto
  settlement index composition — do NOT assume deep); H4 note: small-size
  fill quality overestimates production quality ⇒ staged size ramp with
  re-validation each level (phase 4→5 protocol); H6 note: exposure
  aggregation by FACTOR not market/event (our brackets = single BTC
  factor); mm_scan scoring suggestion: edge × capital-turnover (crypto
  hourly settlement is structural advantage vs sports futures); quote
  funnel counters to catch silent drops (TroyCuban's silent-API-drop bug
  cost him 4x volume for a month); phase-4 review metrics: daily PnL
  skew + worst-day/cumulative-profit ratio; alert tiering (fatigue);
  underfit-over-overfit principle for 1.5C (adverse selection
  asymmetrically punishes overfit quotes).
- blocked / handoff: none; MASTER_SEQUENCE untouched; H7 (rec-flow
  existence in crypto) remains the recommended first test.

## 2026-07-09 ~22:00 UTC — Interview intel #2/#3 + edge-hypotheses synthesis (H1-H12) committed 5811f0d

- commits: 5811f0d (analysis docs #2 P=peanutbettor, #3 K=risktakers-ep3,
  synthesis RESEARCH_EDGE_HYPOTHESES_2026-07-09.md, 2 vendor transcripts,
  prior pending SESSION_LOG entry + BACKLOG notes incl. operator's
  timestamp-ladder note from another session — swept in deliberately with
  operator present). Prior commit this session: b0a8446 (Eggsy analysis).
- operator directives (binding, recorded): (1) docs research notes OK to
  write, NEVER touch execution plans (MASTER_SEQUENCE/PLAN_*) or code;
  (2) interview takeaways are HYPOTHESES to verify, not conclusions —
  hence H1-H12 each with test method + pessimistic pass line; (3) analysis
  docs use "建议" framing, category-ban style decisions belong to operator.
- context capsule: edge decomposition = fee-wall × anchor-lead ×
  rec-flow-existence × ops-discipline; rec-flow existence in crypto (H7)
  is the untested revenue-side multiplier — highest priority, data already
  in warehouse, no timestamp-ladder dependency. H2 (anchor lead-lag)
  depends on timestamp-ladder columns landing first. Ep3 added flow
  taxonomy (H10), one-sided-run adverse-selection test (H11),
  iceberg-vs-queue tradeoff (H12), plus traps: palp-void contagion /
  orphaned hedge leg, exchange adjudication conflict-of-interest.
- RESOLVED: stale-lock problem root-caused (sandbox could create but not
  delete files → git couldn't remove its own lock files). Operator granted
  folder delete permission 2026-07-09 → locks + tmp_obj_* cleaned, future
  git ops from sandbox are self-contained. Mirror sync still not possible
  from sandbox (Desktop not mounted).
- blocked / handoff: none; MASTER_SEQUENCE untouched. Hypothesis testing
  (H7 first) awaits operator go — fits Phase 1 tooling (mm_scan data).

## 2026-07-09 20:15 UTC — Interview intel: top Kalshi sports MM (Eggsy) analyzed, mapped to MM_ROADMAP; 6 backlog items

- commits: b0a8446 "docs: interview intel analysis — top Kalshi sports MM
  (Eggsy), edge/pitfall mapping to MM_ROADMAP" (analysis doc + verbatim
  transcript to docs/vendor/, full-text preservation rule).
- ⚠️ UNCOMMITTED (say-so-loudly clause): this SESSION_LOG entry and the
  docs/BACKLOG.md append could NOT be committed — sandbox left stale git
  locks it cannot unlink and operator declined delete permission. OPERATOR:
  run `cd ~/HFT\ BOT && rm -f .git/HEAD.lock .git/index.lock .git/objects/maintenance.lock`
  then any session can commit. BACKLOG.md also carries an earlier session's
  uncommitted timestamp-ladder note — deliberately NOT swept into b0a8446.
- decisions (all live in docs/RESEARCH_INTERVIEW_EGZEE_2026-07-09.md §5 +
  BACKLOG 2026-07-09 interview-intel entry):
  (1) 6 borrowed items (event-level exposure cap, fair-vs-anchor clamp,
  toxicity-driven quote SIZE, queue-depth in λ calibration, engine heartbeat
  + large-fill alert, external-anchor admission hard rule) merge into their
  owning phases when touched — no new W, MASTER_SEQUENCE not interrupted.
  (2) mentions/politics/weather barred from MM candidate pool until a
  reliable external anchor exists. (3) RFQ/combo making = far-future backlog.
- context capsule: interviewee = solo college MM, ~$900k P&L, 99.9% sports,
  60/40 make/take, fair = scraped sharp sportsbooks (their analog of our
  spot-index anchor — structure validated). His largest losses were
  engineering bugs (flipped-sign scrape, correlated alt-lines aggregating
  $5k→$50k, parlay legs treated independent), NOT model error; his #1
  persistent bleed = faster feeds/courtsiders (= our quote-velocity breaker
  rationale). Kalshi taker fee ~7% is the maker moat; RFQ requester IDs are
  static → counterparty blacklisting works but multi-account via borrowed
  API keys defeats it. Dead end confirmed: quoting markets whose only fair
  is your own book mid ($500 pushes the anchor, then slams you).
- blocked / handoff: none blocking; next session per MASTER_SEQUENCE. Mirror
  sync WARN: /Users/ritcardo/Desktop/TradingSys Report/docs-mirror/ not
  mounted in this sandbox — operator rsync or next session with access.

## 2026-07-09 — W-A4 DONE ✅: ZERO-GAP CUTOVER COMPLETE — EC2 sole owner since 23:09 UTC; trades diff 190,815 = 0/0 missing

- commits: (rest-owner gate) feeeabc · (429 pacing fix) 8e6d703 · (dim_snapshot
  schema-drift fix) + this exit commit. Full evidence:
  docs/plan_audits/wA4_cutover_2026-07-09.md (timeline + diff report +
  no-fabrication table + deviations).
- SEQUENCE AS EXECUTED (operator present, go/no-go ×3): prereqs (delete-denial
  LIVE-tested AccessDenied; egress-443 DEFERRED by operator to before-live S1
  hard gate) → 18:30:57 EC2 WS-only (rest_disabled flag, new supervisor gate,
  red-first contract test) → 29.5-min soak across the hour boundary all green
  (1.54M events, 0 reconnects/drops, rotation correct) → 19:17 Mac REST off
  (flag+reload, 20 s gap EC2-covered) → 19:18–19:29 EC2 REST owner (after
  429 fix) → 23:09:07 Mac unload. WS overlapped the entire 4 h 38 m.
- ACCEPTANCE DIFF (the plan's completeness report): window 18:32–18:59,
  trades by globally-unique trade_id + exchange ts_ms: mac=ec2=190,815,
  ZERO missing either direction ⇒ **seq_gaps counter baseline = 0** (W-C5).
  Ticker channel: symmetric ~11% state differences at equal message volume =
  per-connection CONFLATION (server samples per subscriber), NOT loss —
  proven by zero trade loss on the same TCP stream. NEW FEED FACT for
  kalshi_facts.yaml (queued W-A5/backlog): ticker ≠ lossless event log;
  trades/orderbook_delta are the lossless channels (Q5 implication).
- TWO LIVE INCIDENTS fixed mid-cutover (both required for EC2 REST ownership,
  both red-first + battery green both platforms):
  (1) catalog_sync 429 — EC2's <1 ms RTT ran the pagination loop ~30× faster
  than the Mac's 30 ms RTT and burned the 600-token read burst; the latency
  win itself broke the crawler. Fix: paced_open, 50 ms pacing + exponential
  429 backoff, 5 unit tests (fake opener/clock).
  (2) dim_snapshot Binder Error — fresh crawl's markets parquet had NO
  cap_strike column (union_by_name only materializes present fields); strike
  SQL now built from columns that exist (D3). Regression tests both modes.
  Post-fix proof on box: catalog 4 tables zero-429; DIM SNAPSHOT PASS;
  classification 11,295 series (A=6,194/B=5,101).
- context capsule: EC2 post-cutover healthy (hour-23: 365k events, growing
  +8.5 MB/20 s checked after unload). Mac: launchd plist INSTALLED-dormant,
  pmset disablesleep still ON (do NOT re-enable before W-A5), all data
  intact. Rollback now = load + resubscribe + backfill hole from EC2/S3
  (not instant — by design post-step-5). Diff tool: sandbox/wa4_capture_diff
  (P8). EC2 metrics.ndjson still unbounded (rider (a) pending, 173G free —
  not urgent).
- INDEPENDENT AUDIT: PASS — auditor independently RECOMPUTED the headline
  diff (190,815 trades, 0/0 missing) AND verified within-machine seq
  continuity zero-gaps on both boxes; single-REST-owner never violated
  (log-ordering verified). 6 non-blocking applied: timeline gains the two
  ~30s EC2 restart outages (each Mac-covered; zero-gap phrasing corrected),
  incident-2 timestamp, committed-tool ticker numbers, rollback rest_disabled
  caveat, RUNBOOK post-cutover reality (E5), SIGTERM-kill + events-cap →
  W-A5 queue.
- blocked / handoff: NEXT = W-A5 (fresh session): ① final Mac→S3 delta —
  **Mac-ONLY residual = raw date=2026-07-09 hours 17:00→18:31** (vault ≤16,
  EC2 ≥18:30:57); sync Mac 17–23 anyway (D1) with the
  firehose_<hour>.ndjson* GLOB exclusion lesson; ② daily+prompt EC2→S3 sync;
  ③ detectors/alerts on EC2 + report flow-back to Mac Desktop; ④ cost
  budget section (first real AWS bill = the measurement); ⑤ retire Mac
  sleep-disable; ⑥ riders: metrics rotation (a), full-L1 promotion (b) on
  EC2 config, gitignore CSVs (c); ⑦ vault_staging 85 GB reclaim (operator
  call); ⑧ ticker-conflation fact → kalshi_facts.yaml. Egress-443 = S1 gate
  before first live order (operator ruling, SECURITY_CHECKLIST §4).

## 2026-07-09 — W-A3 DONE ✅: S3 vault live (1,082 obj / 85.52 GB), 1,076/1,076 MD5-verified, restore proven byte-for-byte, ~$2/mo

- commits: f36d60a (vault scripts: sync/verify/restore — never --delete,
  single-part⇒ETag==MD5), restore-script fix commit (cp --recursive silent
  no-op guard), this commit (validation log + plan/playbook/session docs).
- WHAT LANDED: s3://kalshi-vault-ritcardo/mac-vault/{raw,warehouse,meta}.
  Raw FOUR days (07-06 was hours from 3-day age-out — synced FIRST, 11.08 GB;
  07-07 22.27 GB; 07-08 29.51 GB; 07-09 through hour 16, 19.68+1.23 GB),
  warehouse 2.0 GB (facts/dim/catalog/legacy_greed/_meta/manifest.csv), and
  the 6 MD5 account books under meta/. Route: Mac→EC2 rsync (25.9–42 MB/s
  measured) → aws s3 sync on the box (AWS creds never leave the box, S4).
- VERIFICATION (three-hop, every number in the no-fabrication table of
  docs/plan_audits/wA3_s3_vault_2026-07-09.md): Mac `md5 -r` manifests →
  box `md5sum -c` (BAD=0 all components) → S3 ETag==MD5 per object
  (VERIFY 0 mismatches ×5, total 1,076). RESTORE TEST both legs: biggest raw
  file (663,175,728 B) and an archive parquet partition — S3→box→Mac `cmp`
  rc=0 AND the parquet md5 == manifest.csv file_md5 (literal acceptance).
- THE CATCH THAT PROVES THE CHECKS WORK: hour-16 retry segment
  firehose_16.ndjson.4 was rsynced MID-APPEND → hop-1 md5 flagged it (BAD=1);
  refreshed after the hour closed ⇒ BAD=0, 84/84 in S3. LESSON FOR W-A5 delta
  sync: exclude firehose_<current-hour>.ndjson* (GLOB — main + segments).
- Cost measured: 85.52 GB × $0.023 = ~$2.0/mo + one-off pennies; within the
  W-A0 S3 budget row. Versions from the refresh ≈1.2 GB noncurrent (delete
  is impossible for vaultWriter — operator-only, separate creds).
- Operator prerequisites consumed: bucket kalshi-vault-ritcardo (versioned),
  IAM no-deletion-ritcardo (List/Get/Put only), vaultWriter keys in box
  env.sh, awscli(snap), **Elastic IP 3.130.232.109** (git remote `ec2` +
  RUNBOOK updated from the old 13.59.9.97).
- tooling incidents (fixed in-session, committed): macOS rsync rejects
  --info=stats2 (use --stats); aws s3 cp --recursive on an exact key is a
  SILENT NO-OP (exit 0, zero files) — restore script now tries single-object
  first + zero-file guard.
- INDEPENDENT AUDIT (no-fabrication sweep): PASS on substance, ZERO fabricated
  numbers (auditor re-measured every reachable figure, all matched; plus a
  full 699-row manifest.csv file_md5 cross-check vs S3 — 0 mismatches). ONE
  blocking CITATION defect (B1): 07-06 verify lines predated the tee’d log —
  re-run + appended 17:43Z, doc citations corrected. Also applied: du -sb as
  the durable row-2 artifact; delete-denial relabeled UNTESTED and queued as
  a W-A4 go/no-go item (witnessed aws s3 rm ⇒ AccessDenied); GiB-vs-GB cost
  nit noted ($1.83–1.97 bracket); multipart>2GB fail-closed note for W-A5.
- blocked / handoff: NEXT = **W-A4 zero-gap cutover — operator MUST be
  present (go/no-go)**. Remaining prerequisite: egress-443 lockdown
  (SECURITY_CHECKLIST_EC2.md §4–5); EIP ✅ done; work/live ✅ exists.
  ~/vault_staging (79 GB) on the box = redundant third copy, keep until
  W-A4 done. Mac untouched all session (P4): zero deletions, capture ran
  throughout, only reads + a second WS connection never opened this session.

## 2026-07-09 — W-A2 DONE ✅: full battery green ON EC2 (50/50), REST+WS preflight from AWS passed; 3 portability fixes

- commits: 2f0c1b3 (Makefile fuzz ignorelist clang-conditional + bringup
  pyyaml) · this commit (validation log + plan/playbook/session docs).
  Same-session continuation after W-A0/W-A1 (operator-initiated resume).
- ACCEPTANCE (all demonstrated, evidence in
  docs/plan_audits/wA2_linux_validation_2026-07-09.md + ~/wA2_validation.log
  on the box): make check exit 0 ON the box; tests/run_pipeline.sh
  `PIPELINE PASS` 50/50 suites zero fails; check_registry --require-built
  green; operator-run preflight --prod-ok exit 0 (REST, external-api host) +
  10 s read-only ws_shadow WS SHADOW PASS — 5,815 events (578 trade/5,237
  tick), reconnects/errors/drops all 0, transmitted=0, capture to isolated
  work/probe/ (never work/raw).
- DECLARED DEVIATION: plan said "10 s ws_smoke" but ws_smoke is mock-only by
  design (hardcoded fake signer — apps/ws_smoke.cpp); intent satisfied with
  ws_shadow. Recorded in the validation log for the audit.
- 3 PORTABILITY FIXES (test-only, W-A2 allowed writes):
  (1) pyyaml missing from venv — `import yaml` is lazy (function-body) in
  mm_research/build_classification, invisible to top-level import scans;
  (2) fuzz_decode/account_info/rate_probe/account_upgrade not in default make
  target ⇒ check_registry --require-built failed on fresh box — bringup now
  builds them (fixed by direct make targets this session);
  (3) -fsanitize-ignorelist= is clang-only ⇒ FUZZ_IGNORELIST conditional in
  Makefile; PROOF: make fuzz 200k iters clean on box/g++ (simdjson inlines
  instrumented!) AND Mac/clang. Re-prove after simdjson upgrades.
- context capsule: box HEAD 2f0c1b3; venv now duckdb==1.4.5/numpy/pandas/
  pytest/pyyaml-6.0.3; clang-18+libstdc++ lacks std::expected (that's why
  fuzz couldn't just use clang); preflight without KALSHI_ENV=prod correctly
  fail-closes to local_mock (operator's first attempt demonstrated it — the
  fail-closed default working as designed, S2). Probe artifacts in work/probe/
  are disposable. wc=5,817 lines captured in 10 s ≈ 580 msg/s whole-market
  firehose at that hour (useful datapoint vs the 472 ev/s day-mean).
- INDEPENDENT AUDIT: ✅ PASS, 0 blocking, 4 non-blocking (evidence hygiene) —
  all applied/queued; details in the validation log’s "Independent audit
  outcome" section. New binding practice: operator-run preflight commands
  tee output to a durable on-box log (~/wA<N>_preflight.log).
- blocked / handoff: NEXT = W-A3 (S3 vault + RESTORE TEST). OPERATOR
  PREREQUISITES for W-A3 (S4): create S3 bucket (versioned) + IAM user per
  the plan's least-privilege rules (Put/Get/List on vault prefixes, NO
  DeleteObject, no lifecycle-expiry on raw/archive/catalog) + put the keys in
  the box env.sh by hand. Pre-W-A4 items unchanged: Elastic IP + egress-443.
  Mac pipeline untouched again this session (P4; the only prod-adjacent
  action was the 10 s second WS connection — exempt per 3-conn test).

## 2026-07-09 — W-A0 CLOSED + W-A1 DONE: EC2 box hardened, make check green both platforms, capture NOT started (correct)

- commits: df13191 (deploy/ artifacts: systemd units, oom-guard, bringup,
  security checklist, README_EC2) · d231452 (env.hpp <cstdint> portability fix
  + RUNBOOK EC2 section + W-A0 closure) · this commit (W-A1 RESULT + docs).
- W-A0 CLOSED (measured over SSH): nproc=2 / 16 GiB / 200 GB nvme / aarch64 /
  Ubuntu 24.04.4 on i-0fd427becf740a06b @ 13.59.9.97 — matches operator-picked
  option 1 (r8g.large). Recorded in PLAN_AWS_MIGRATION "W-A0 RESULT".
- W-A1 DONE (operator 判定收官 after running the auth smoke themselves):
  - Code path: Mac pushes to bare ~/kalshi.git over SSH → ~/hft-bot worktree.
    NO GitHub credentials on the box, ever (S4). git remote name: `ec2`.
  - ONE portability fix unblocked the whole Linux build: env.hpp missing
    #include <cstdint> (libc++ transitive vs libstdc++). Zero behavior change;
    make check exit 0 on BOTH platforms at the same commit. (Scope note: a
    W-A2-class fix pulled forward by the operator's "make check on EC2" ask.)
  - Compiler FACT: binaries are g++ 13.3.0 (readelf-verified). Makefile's
    `CXX ?= clang++` never fires on Linux (make built-in CXX=g++ wins). Kept
    deliberately; clang installed as fallback. bringup echo corrected.
  - Guardrails live: 16 GB swap, oom-guard timer (ws_shadow −1000 / batch
    +300+nice+ionice), unit OOMScoreAdjust −600, chrony→Amazon Time Sync
    offset 6.9 µs, UTC, unattended security updates.
  - kalshi-pipeline.service installed+disabled+inactive — FIRST START IS
    W-A4's (single-writer discipline; Mac remains the only capture).
  - Credentials: operator scp'd env.sh(127B)+private_key.pem(1679B), 600;
    agent's attempt to source env.sh was permission-blocked (correct, S4);
    operator ran `preflight --prod-ok` ⇒ PREFLIGHT PASS (order line untested,
    no --order — as designed).
- decisions → files: W-A1 RESULT + two operator-ruled deviations (Elastic IP
  未分配 + egress 未收紧, BOTH due before W-A4 as cutover prerequisites) in
  PLAN_AWS_MIGRATION.md; playbook updated; RUNBOOK §5 EC2 ops; key now at
  ~/.ssh/kalshi-key.pem (400).
- context capsule: box repo may hold untracked .venv/ + tools/__pycache__/
  (harmless; consider .gitignore with rider (c)). bringup_ec2.sh is idempotent
  — rerun after any teardown. ssh alias pattern:
  `ssh -i ~/.ssh/kalshi-key.pem ubuntu@13.59.9.97`. Box disk 20/193 GB.
  Preflight defaults to external-api.kalshi.com (the us-east-2-native host,
  W-A0 region note) — no BASE_URL override needed on EC2.
- INDEPENDENT AUDIT (post-entry, same session): verdict FAIL-until-fixed —
  **1 BLOCKING (B1): work/ is gitignored ⇒ fresh clone has no work/live/, and
  systemd opens the unit's StandardOutput=append: log BEFORE ExecStart without
  creating parent dirs ⇒ `systemctl start` would die status=209 and crash-loop
  every 30 s at W-A4** (auditor proved the semantics empirically on the box
  with a throwaway /bin/true transient unit). FIXED (commit d3aa540): mkdir in
  bringup step 7 + mkdir applied on the box NOW + W-A4 gained a PREREQUISITES
  block (EIP, egress-443, work/live exists) so the go/no-go re-checks all
  three. 3 non-blocking also applied: W-A4 prerequisites recorded in W-A4's
  own section (E5); checklist notes VPC-resolver SG bypass + that the egress
  lockdown silently stops unattended-upgrades (http/80). Auditor verified
  clean: env.hpp diff exactly one line; Restart semantics (exit 0 lock /
  exit 78 config) correct; KillMode no worse than launchd; oom_guard regex
  covers all 10 supervisor python invocations; Mac capture alive throughout
  (P4); no credentials in any diff (S4); make check re-run green by auditor.
- blocked / handoff: NEXT = W-A2 (fresh session): full battery on the box
  incl. tests/run_pipeline.sh + check_registry + ONE read-only preflight from
  EC2 IP (already de-facto smoked) + 10 s ws_smoke. Before W-A4: operator
  does EIP + egress-443 (checklist §4–5, item 11). Mac pipeline untouched all
  session (P4: zero capture risk — nothing on the Mac was stopped/started).

## 2026-07-08 — W-A0 DONE (paper): r8g.2xlarge/64GB/us-east-2/300GB gp3 ≈ $380/mo; boot checklist ready; audit PASS

- commits: f8cff37 (sizing decision + operator boot checklist) + this commit
  (audit remediation, 6 non-blocking findings applied). Paper only — writes were
  PLAN_AWS_MIGRATION.md "W-A0 RESULT" + playbook bookkeeping; no code/config/
  production touched; no money spent (the money gate is the operator clicking
  Launch).
- decisions (all in docs/PLAN_AWS_MIGRATION.md → "W-A0 RESULT"):
  (1) BRANCH: ≥32 GB locked (operator) — gold builds on EC2; Mac retires from
  the production pipeline post-W-A5 but keeps the report-landing job + rollback
  capability (NOT wipeable); W-A5 <32 GB exception paragraph declared n/a.
  (2) REGION: us-east-2 — proven by dig: external-api(-ws).kalshi.com CNAME to
  an us-east-2 ELB; from EC2 prefer external-api hosts (api.elections… is
  CloudFront); PrivateLink exists as a future option.
  (3) INSTANCE: r8g.2xlarge (8 vCPU/64 GB Graviton4) $344/mo on-demand
  ($228 1-yr reserved — purchase deferred to W-A5), sized for STEP 4 full-depth
  worst case (~85 GB raw/day, 3-day window ≈250 GB) where the gold build's
  memory scaling is an honest unknown (today: ru_maxrss 6.33 GB / 17.7 GB peak
  ON A 16 GB MAC — hw.memsize measured this session, macOS swap absorbed it).
  64 GB kills the gamble AND avoids a future resize = capture gap. ARM chosen
  because the dev Mac is arm64 (codebase already green on ARM); x86 fallback
  r7i.2xlarge $386 if W-A1/A2 surprises.
  (4) EBS: 300 GB gp3 $24/mo at launch (≥2× headroom incl. 3g conservative),
  grow ONLINE to 500 GB before STEP 4 N≥200 (gp3 grows with no downtime).
  (5) BUDGET LINE: ~$380/mo at launch (incl. the post-2024 ~$4/mo public-IPv4
  charge the audit caught) → ~$260/mo if reserved.
- context capsule: pricing = ec2.shop API us-east-2 2026-07-08 (audit re-fetched,
  all cells verified; console quote is final truth at launch). Depth numbers =
  PLAN_DEPTH_EXPANSION §3 brackets, PROBE-PENDING. 3e/rider(b) adds NO raw bytes
  (capture already stores all categories; ingest was discarding Class B) — only
  ingest/gold rows grow ~1.3–2× [ESTIMATE]. Firehose raw 21–22 GB/day measured
  on gap days; re-baseline after first clean EC2 day (07-08 was 23 GB and still
  accruing at audit time). Independent audit: PASS, 0 blocking, 6 non-blocking
  ALL applied (EIP pricing outdated→fixed+budget row; round-up 85/250; bracket
  stated; 退役 scope reworded both files; orphan-volume gotcha on the x86
  fallback path; re-baseline note).
- blocked / handoff: NEXT = operator follows the boot checklist (12 steps,
  Chinese, in the plan doc) — THE money gate, ~$0.47/hr starts at step 10 —
  then sends back the public IP + SSH confirmation. Next session = W-A0 closure
  (3 read-only cmds: nproc/free -g/lsblk, expect 8/62-64/300) rolled into W-A1
  start (hardening & bring-up). Do NOT create IAM/S3 yet (that's W-A3's
  prerequisite). Interim pmset disablesleep stays ON until W-A5.

## 2026-07-08 — PLAN_AWS_MIGRATION.md authored, independently audited, 8 findings applied (paper, P9)

- commits: f652046 (full 7-field draft) · 4c6ebe3 (audit remediation). Paper-only
  session; NO production/live code touched; execution stays gated on the operator's
  AWS account (S4). This is MASTER_SEQUENCE STEP 1's plan, now execution-ready.
- WHAT THE PLAN IS: relocate the whole capture+warehouse pipeline off the sleeping
  Mac onto an always-on Linux EC2 host — the permanent fix for the sleep root cause
  proven 2026-07-08 (see the CAPTURE ROOT CAUSE entry below). Six Ws, one per fresh
  session, independent audit after each:
  W-A0 provision/size · W-A1 build+systemd+firewall · W-A2 test battery green on EC2
  · W-A3 S3 vault + PROVEN restore · W-A4 zero-gap cutover (operator go/no-go) ·
  W-A5 detectors/cost/report-flowback + retire interim pmset.
- CUTOVER SAFETY (P4/D1): WS runs on BOTH boxes through steps 1–4 so capture never
  drops; Mac launchd stays installed-but-dormant = rollback host. SINGLE REST OWNER
  rule is operator-locked (never two REST pollers; WS concurrency exempt per
  2026-07-06 3-conn test).
- AUDIT: independent agent verdict = NO BLOCKING DEFECTS. 8 non-blocking findings
  applied (commit 4c6ebe3, full list in the commit body). The load-bearing ones:
  (1) W-A5's FIRST action is a final incremental Mac→S3 sync of the W-A3→W-A4-step-1
  residual window (captured only on Mac, else lost) with byte/md5 re-verify, BEFORE
  the Mac is disposable; (2) EC2 seeds subscriptions from the S3-vaulted catalog, no
  REST until step 4, so single-owner is never momentarily broken; (3) honest
  rollback — post-step-5 is NOT instant and must backfill the unload→reload hole
  from EC2/S3; (4) least-privilege IAM — EC2 role has NO DeleteObject on vault
  prefixes, making D1 structural; (5) W-A0 sizing must carry rider (b)'s expanded
  full-market L1 universe and reconcile the ≥32GB (Mac dormant) vs <32GB (gold on
  Mac, sleep NOT fully re-enabled) branches; (6) three systemd load-bearing
  behaviors spelled out (hourly rotation, single-instance lock, Restart=on-failure
  NOT WatchdogSec).
- BLOCKED / HANDOFF: execution needs the operator to answer "AWS account ready, or
  is spinning one up move #1?" then create account + IAM + S3 bucket + hand-create
  ~/.kalshi/env.sh on the box (all S4, operator-only) and be present for W-A4.
  Interim mitigation still LIVE: operator ran `sudo pmset -a disablesleep 1` (do NOT
  re-enable sleep until W-A5 post-cutover; verify `pmset -g | grep SleepDisabled`).
- NEXT: W-C5 production session (data-silence watchdog + 3 counters) OR STEP 1 AWS
  W-A0 once operator confirms account — operator's call.

## 2026-07-08 — CAPTURE ROOT CAUSE PROVEN = Mac sleep; caffeinate mitigation; AWS-plan seed; dashboard→ET

- CAPTURE IS NOT 24/7 BECAUSE THE MAC SLEEPS. Operator 62-gap scan (subscribed
  record within 60s of gap-end) → 58 reconnect-recovery + 4 same-connection
  data-dropout. pmset -g log then PROVED all sampled gaps align 1:1 to Deep-Idle
  Sleep→Wake: cases 07-06 12:26/14:48 (~62s = short sleeps), 07-07 17:29→17:51
  (22.7min sleep cascade); the ~15-min "wedge" IS the DarkWake cycle (07-08
  05:13-09:59 = 12 back-to-back ~15-min sleeps; 50 sleep/wake transitions today).
  Lease-TTL hypothesis DISPROVEN. Full writeup + appendix:
  docs/plan_audits/capture_gap_taxonomy_2026-07-08.md. 07-08 was 36.7% DOWN.
- MITIGATION (operator ruling): `caffeinate -dimsu` launched detached/user-level
  (pid at exit; revert `kill <pid>` or when superseded) — TEMPORARY until the
  operator's persistent `sudo pmset -c sleep 0` / AWS migration. This should stop
  most gaps immediately.
- SECONDARY (real but not root cause): W-C1 watchdog is ping-masked —
  last_activity_ms_ bumped by ping/pong (ws_client.cpp:114), so ping_silent(20s)
  never trips on a live-connection data hole; it fires at ~901s. FIX (W-C5,
  production, its own full-discipline session per playbook 3b): data-frame-silence
  trigger (last_data_ms_ on Text only) → resubscribe-on-live-socket then escalate
  to force_reconnect; BUNDLED with 3 atomic observability counters (seq_gaps /
  ring high-water / simdjson-fail) in the SAME code+red-first-test+audit. NOT done
  this session (operator ruling).
- AWS: seeded docs/PLAN_AWS_MIGRATION.md (full 7-field W queue stays playbook
  item 4, gated on AWS account, S4) with the operator's W-A4 provisions: SINGLE
  REST OWNER rule + cutover sequence (EC2 WS→verify→Mac stops REST→EC2 REST→Mac
  unload; WS concurrency exempt per 2026-07-06 3-conn test) + dual-machine ws_seq
  diff acceptance → first capture-completeness report = seq_gaps baseline.
  Operator added W-A5 PDF-report-flowback (S3→Mac Desktop).
- DASHBOARD: sandbox prototype now displays Eastern (America/New_York, DST-auto);
  canonical data + partition dates stay UTC (D1). W-D1 design APPROVED + frozen
  (operator, separate entry). RESEARCH_METRICS_BRIEF.md audited (no blocking).
- NEXT: operator runs persistent pmset; W-C5 production session (watchdog +
  counters); STEP 1 AWS. SINGLE-OWNER RULE in force.

## 2026-07-08 — RESEARCH_METRICS_BRIEF.md drafted + independently audited (paper, P9)

- commit f4a72a5 (draft) + remediation (this commit). For operator research day.
- Content: (A) MM monitoring metrics menu by our 4 lifecycle phases (collection/
  calibration/shadow/live), 16 metrics each with question+anomaly-action+formula+
  data+when-computable, tagged VALIDATED/PARTIAL/LIT + a GUARDRAILS Q1-Q9 anchor;
  (B) reference frame — rodlaf autopsy lessons (P5) + Avellaneda-Stoikov
  structure-only with 3 mandatory adaptations (log-odds Q1 / event-not-diffusion
  risk Q8 / expiry boundary Q6); (C) two-week agenda by unlock condition (now /
  clean-days / backfill) with deliverables. No fabricated params (Q4).
- INDEPENDENT AUDIT (combined, P9): NO BLOCKING DEFECTS. Verified against
  mm_calibrate.py (markout=tox_lo signed log-odds mid-move; edge_after_tox),
  mm_backtest.py (pessimistic/optimistic bound defs; fees/settlement NOT modeled),
  GUARDRAILS Q1-Q9 anchors, MM_ROADMAP phases, ARCHITECTURE_REVIEW (+8-9¢ cite is
  real, not invented). Remediated its 2 non-blocking + nits: (1) C2 "net of fees"
  reworded — backtest hardcodes maker-0, Q3 fee model is a prerequisite; (2) added
  a units note that avg_spread/edge_after_tox are price-space (cents) vs the
  log-odds markout; spread-capture re-anchored Q3/Q1; self-fill anchor → S6 only.
- verified: doc committed; no code touched (pure paper). Ready for research day.

## 2026-07-08 — W-D1 APPROVED (written, with operator addendum ⑥⑦) — design FROZEN

- commit: (this commit) dashboard_design_wD1.md gains OPERATOR ADDENDUM
  (⑥ live·partial today column w/ day-end reconciliation; ⑦ per-panel
  data-layer + cadence annotation) and the written OPERATOR APPROVAL block.
- decisions (file: docs/plan_audits/dashboard_design_wD1.md): W-D1 STOP
  cleared. Four-zone IA un-split; contracts ①–⑦ binding; productionisation
  ①→④→②→③ riding W-D2→D3→D4/D5; design frozen — further changes need an
  operator-approved amendment.
- context capsule: approval given after doc verification (five contracts
  confirmed present at 59f2a1c, 227 lines; two gaps found by grep —
  live·partial column and data-layer annotation had 0 hits — folded in as
  addendum rather than another resubmission round). W-D6 still blocked by
  W-D2..D5 (B1: collectors on EC2 post-cutover). Prototype remains
  sandbox-only, manual-run (P8).
- blocked / handoff: dashboard track now idle until STEP 1 cutover; next
  actionable items remain playbook 2a (supervisor restart after 07-09
  00:00 UTC), item 2 (W-C3 acceptance), W-C5 fix (critical path to
  clean-days gate), and the two research Ws (event packaging W-E0/E1,
  pricing-model math skeleton) which are unblocked NOW.

## 2026-07-08 — W-D1 prototype COMPLETE (sandbox, P8): decision-driven Q1–Q4, ready for approval review

- sandbox/obs_server.py + obs.html: all four operator-question zones live on REAL
  data. ① healthy-now (proc liveness, rate-vs-multi-day-baseline w/ auto-degrade,
  channel breakdown, disk) · ② data-usable (coverage matrix w/ per-cell median
  baseline, latency p50/p95/p99 distribution, 7-clean-days evidence, integrity) ·
  ③ gates · ④ incident forensics (capture_gaps + quality_log on one timeline;
  click→jump; CASE #1 pinned = 07-08 02:00 gap = candidate W-C5). Provenance: click
  any ⌕/incident → source-record drawer. uPlot vendored (third_party, pinned), SSE
  push. Granularity + decision-IA written into docs/plan_audits/dashboard_design_wD1.md.
  DESIGN DOC COMPLETED per operator: five scope contracts specified — ① analytics
  identity contract + generic renderer, ② data catalog browser + DuckDB four rails,
  ③ activity heatmap day/week/month + activity_daily rollup + gap-hatch, ④ event
  timeline (blocked-by W-E1; close_time≠start D2 warning), ⑤ markout/toxicity
  formula+stages — each BUILT or DEFERRED w/ blocked-by. Decisions: four-zone flow
  un-split; productionisation ①→④→②→③ per W-D2→D3→D4/D5.
  STOP: RESUBMITTED — awaiting operator WRITTEN W-D1 approval before dashboard_server.py (W-D6).

## 2026-07-08 — W-D1 dashboard prototype (sandbox, P8 free-fire): live observatory, uPlot + SSE

- sandbox prototype `sandbox/obs_server.py` + `obs.html` (read-only, localhost,
  manual-run only, no order/panic — S5): live Overview driven by REAL files
  (metrics.ndjson feed series, capture_gaps.csv, capture_alert.json,
  lifecycle_status.json) — zero fabricated data. Rebuilt to the DESIGN SYSTEM
  RULES (c5d45bb): flat dense terminal, status strip + small multiples (shared
  axis, min/avg/max), gap TIMELINES (00:00-24:00, true positions; 07-06 hatched
  capture-start), rates-not-cumulative, zero-floored axes, metrics tape. v1 static
  mock REJECTED by operator (grey/fake) — deleted. v4: charts = **uPlot**
  (vendored third_party/uplot v1.6.30, pinned, no CDN) box-zoom/crosshair-synced;
  data via **SSE** /stream (not polling). Reconciled: freshness<0 = clock jitter,
  floored; 07-08 "2 gaps" VERIFIED real (00:00 midnight + 02:00 W-C5). Design doc:
  docs/plan_audits/dashboard_design_wD1.md. STOP: awaiting operator written W-D1
  approval before any dashboard_server.py (W-D6) code.

## 2026-07-08 — GUARDRAILS P7-P9: three friction relaxations (operator-approved); safety fuses untouched

- commits: GUARDRAILS P7-P9 (own commit per its change rule); this entry.
- decisions (file: docs/GUARDRAILS.md §5): P7 reads never need enumeration,
  only writes; P8 sandbox/ = free-fire zone (no W/audit/full ritual; production
  may never import from it); P9 paper-only Ws batchable per session, one
  combined audit. Explicitly NOT relaxed: S1-S6, D1-D6, supervisor/capture
  gates, independent audits for anything non-sandbox.
- context capsule: prompted by operator ("放松没必要的权限提升流畅度").
  Principle applied: freedom where mistakes are cheap+reversible, rigor where
  irreversible/lie-prone. Evidence anchors: A1 doc-bug came from over-tight
  read lists (P7); dashboard prototype iteration already worked sandbox-style
  de facto (P8); W-D0 paper audit found 11 real issues so audits stay (P9).
- blocked / handoff: dashboard design iteration continues in sandbox under P8;
  queue unchanged (playbook 2a supervisor restart + item 2 after 07-09 00:00
  UTC; W-D1 approval still the STOP before W-D6).

## 2026-07-08 03:45 UTC — Playbook item 1 DONE: supervisor daily wiring (W-C2.1 + coverage_audit) + audit

- commits: 9878025 (W-C2.1 capture_gaps daily+live), 7ced1a7 (coverage_audit
  daily wiring + W-C2.1 audit fixes). Discharges OPERATOR_PLAYBOOK item 1 +
  next_actions.md item 1 (both checked off this session).
- NOTE ON SCOPE: the pasted prompt was only the W-C2.1 half; the playbook defines
  item 1 as W-C2.1 + coverage_audit COMBINED (one supervisor P4 review). Completed
  the full item so the supervisor is touched once, not twice.
- WIRED into tools/pipeline_supervisor.sh (all read-only, write only derived
  artifacts; P4 continuity preserved — see commit messages):
  - daily export block: `capture_gaps.py --date "$YESTERDAY"` (durable gap record
    before 3-day raw prune) + `coverage_audit.py --date "$YESTERDAY"` (non-zero exit
    = V15 depth shrinkage, surfaced to supervisor.out.log). Both backgrounded after
    the pause lifts, so neither delays the ws_shadow relaunch.
  - 60s watchdog loop: `capture_gaps.py --live` -> work/live/capture_alert.json.
- INDEPENDENT AUDIT of the capture_gaps wiring: NO BLOCKING DEFECTS. Verified
  no-hang, no set -e abort (script is set -u only), no prune race (YESTERDAY raw is
  1-day old vs 3-day retention), correct UTC, live-file-edit-safe. Non-blocking
  fixes applied: (#1) capture_gaps._last_us now TAIL-SEEKS the final 64 KiB instead
  of scanning the whole ~0.5 GB current segment every 60s (--live now O(1));
  (#5) the wiring contract test now checks NON-COMMENT lines. Nits left: no
  `timeout` guard (macOS lacks timeout; tail-seek makes hang risk negligible),
  unbounded capture_gaps.log (matches other work/live logs), transient false
  hour-rollover alert (cosmetic, dashboard-only).
- NOT ACTIVE until the next supervisor restart: the running supervisor (PIDs
  4314/4323) already parsed its while-loop, so this file edit doesn't disturb it
  AND doesn't take effect until it restarts (launchd crash-recovery or manual). I
  did NOT restart it (production action = operator's call). Record already populated
  07-06..07-08 from this session's manual runs. OPERATOR RULING 2026-07-08: do NOT
  restart before 2026-07-09 00:00 UTC — restarting now stamps a fresh gap onto
  07-08, the W-C3 measurement day (expected: ONLY the known 00:00-00:10 hole).
  Zero cost to waiting (record backfilled, retention clock safe). Post-restart
  verify: (i) work/live/capture_alert.json appears within 60s; (ii) the restart's
  own small gap shows in 07-09's record (the detector logging its own restart =
  the wiring working honestly). See OPERATOR_PLAYBOOK item 2a.
- verification: make check GREEN; run_pipeline PIPELINE PASS (capture_gaps,
  coverage_audit, pipeline_contract); bash -n OK; registry 121.
- CONTEXT for a fresh session: a PARALLEL effort landed on this branch during this
  work — PLAN_DASHBOARD_OBSERVATORY.md (W-D0..D7, dashboard redesign) + W-D0 audit
  remediation + OPERATOR_PLAYBOOK.md. Next playbook items: 2 = W-C3 acceptance tail
  (TIME-GATED, run after 2026-07-09 00:00 UTC); 3 = W-D1 dashboard design mockups
  (STOP for operator approval before any dashboard code). Still open from earlier:
  W-C5 (hour-boundary non-zero-exit gap, BACKLOG 07-08). Branch pushed to
  origin=github.com/Ritchieritch2025/HFT-BOT. SINGLE-OWNER RULE in force.

## 2026-07-07 — OPERATOR_PLAYBOOK.md created: full queue as paste-ready per-session prompts

- commit: (this commit) docs/OPERATOR_PLAYBOOK.md.
- decision (file: OPERATOR_PLAYBOOK.md): W-C2.1 + coverage_audit supervisor
  wiring COMBINED into one session (same file, same risk class, one P4
  review); operator approval for the supervisor edit is granted by pasting
  playbook item 1 (satisfies next_actions.md item-1 gate).
- context capsule: playbook = convenience layer over MASTER_SEQUENCE (which
  stays authoritative); items 1–4 have verbatim prompts, 5+ point at their
  plan docs; "only-you" list (AWS account, cutover go/no-go, W-D1 approval,
  OQ-1, S1) and standing gates restated. Playbook maintenance added to exit
  ritual expectations via E5 note in the file header.
- blocked / handoff: operator's next action = paste playbook item 1 into a
  fresh session. Item 2 time-gated until 2026-07-09 00:00 UTC.

## 2026-07-07 — W-D0 audit remediation: 11 findings confirmed + fixed; 401 evidence archived; B1 ruling OPEN

- commit: (this commit) plan amendments + docs/plan_audits/
  dashboard_wD0_requirements_audit_2026-07-07.md (audit VERBATIM + verdicts)
  + tests/fixtures/incidents/ (archived 401 evidence).
- decisions (file: PLAN_DASHBOARD_OBSERVATORY.md unless noted):
  - A1: W-D6 Allowed reads explicitly include capture_gaps.csv,
    capture_alert.json, metrics.ndjson — no mirroring layer.
  - A2: notify.json contract added to W-D3; refresh cadence = 5s poll;
    "one refresh cycle" = ≤10s, objectively judgeable.
  - A3: clean-day now requires POSITIVE coverage evidence (scan record +
    continuous metrics + zero gaps); silence never advances the counter.
  - A4: artifact envelope rule (schema_version/generated_at_us/source_sha/
    max_age_s; stale ⇒ UNKNOWN); W-D4 60s + W-D5 10min supervisor cadence;
    W-D7 stop-collector degradation test.
  - B2: live-statistic vs state definition (TREND RULE now enforceable).
  - B3: MODULE NOT LIVE = banner-exempt honest state; green-washing a
    scaffold = named D2 violation.
  - B4: backtest contract pinned — schema_version 1, ts int64 µs UTC, money
    E4 cents, qty whole contracts, no floats.
  - C1 (URGENT, DONE): real 401 evidence archived to tests/fixtures/incidents/
    (full ws_shadow.log copy, 886KB, 488 401-lines + quality_log slice) —
    raw/log rotation would have destroyed it before W-D3 executes.
  - C2: RESOLVED-GOOD — lifecycle_status.json/lifecycle_events.ndjson already
    exist (lifecycle_check.py); W-D4 reads them; sha-absent ⇒ UNKNOWN.
  - C3: forced_reconnects_ verified NOT in metrics.ndjson; log-parse stays as
    pinned fallback; metrics-export rider proposed (capture-side, D4 test
    same change, operator-gated).
  - C4: catalog (path,size,mtime) count cache + IO-budget acceptance;
    latency_daily.ndjson downsample so long trends survive 512MB×3 rotation.
- context capsule: audit source = operator-relayed independent review of
  commit 0326a59. All A/B findings verified true against the doc; C2 was the
  only inaccuracy (record already exists). Verification evidence: 401 replay
  previously existed ONLY as unit-test pattern (test_ws_client.cpp:159-186)
  + live log; forced_reconnects at ws_client.hpp:100,142; lifecycle files
  confirmed by reading tools/lifecycle_check.py:4-27.
- blocked / handoff: B1 RULED same day (operator, follow-up): W-D2..D5 wait
  for STEP 1 cutover, built on EC2 — recorded in plan header + W-D2.
  Next dashboard W = W-D1 static design mock
  (STOP), now unblocked since B2/B3 renderings are specified. Wider queue
  unchanged: STEP 1 AWS + W-C3 acceptance tail + W-C2.1 wiring.

## 2026-07-07 — W-D0 DONE: dashboard requirements approved + PLAN_DASHBOARD_OBSERVATORY.md drafted

- commit: (this commit) docs/PLAN_DASHBOARD_OBSERVATORY.md — the previously
  referenced-but-nonexistent plan for MASTER_SEQUENCE STEPs 2–3 now exists.
- decisions (all operator-approved 2026-07-07, recorded in the plan §2):
  1. Keep dashboard_server.py backend skeleton (audited S5/E3 properties);
     rebuild frontend as modular tabs + shared component kit.
  2. Execution tab strictly read-only; NO panic button (S5 stands as written);
     dashboard shows kill-switch state + copyable panic CLI line only.
  3. Visual: dense terminal dark (GitHub-dark palette, tabular-nums, sparklines).
  4. W-D6 launch scope: Overview + Data tabs fully functional; Backtest/
     Strategy/Execution as D2-honest scaffolds with fixed data contracts.
- context capsule: 7 tabs (Overview/Data/Backtest/Strategy/Execution/Tests/
  Tools) specced with data sources in plan §2 table. Backend Ws defined:
  W-D2 latency/feed-health collector (work/observatory/latency.ndjson),
  W-D3 incident detector (acceptance = real 2026-07-07 401-segment replay),
  W-D4 readiness snapshot (fail-closed: missing input ⇒ UNKNOWN ⇒ NOT-GO),
  W-D5 warehouse catalog (the "path to all data pulled"). Backtest result
  contract fixed in plan §4 (E4 prices, pessimistic bound headline) so the
  future engine targets it. TREND RULE restated as binding: bare live number
  = acceptance FAIL. Sequencing: W-D0/D1 gate only W-D6; STEP 1 (AWS) still
  precedes implementation Ws. Dead end ruled out: full server rewrite
  (rejected — would re-audit S5/E3 for no functional gain).
- blocked / handoff: NEXT for dashboard = W-D1 static design mock (STOP:
  operator approves in writing before any W-D6 code). Overall queue still at
  MASTER_SEQUENCE STEP 1 (AWS) + W-C3 acceptance tail (24h zero-gap on
  2026-07-08) + W-C2.1 daily capture_gaps wiring (urgent, 3d raw retention).
  Pre-existing dirty state NOT touched by this session: modified
  config/*.csv (gitignore rider, STEP 1), untracked sandbox/discovery/*.

## 2026-07-08 00:55 UTC — W-C2 independent audit: 3 BLOCKING false-negatives + a crash, ALL FIXED

- commit: d0890f4 W-C2 audit remediation. Writeup:
  docs/plan_audits/capture_wc2_audit_2026-07-08.md.
- Independent fresh-context audit of e455f99 found 3 BLOCKING false-negative
  defects (a gap detector that MISSES a real hole lets a holed tape pass as
  complete — the dangerous direction). All real, all fixed + red-first pinned:
  - B1 DAY-BOUNDARY/EDGE holes silently missed: find_gaps needed a consecutive
    record PAIR, so a hole crossing midnight (last rec in date=D/, first in
    date=D+1/) — plus head/tail-of-day silence — was seen by NEITHER day. FIX:
    scan_date now emits LEADING (day_start->first) + TRAILING (last->min(day_end,
    now)) edge gaps; a midnight hole = trailing gap of D + leading gap of D+1;
    `now` cap avoids flagging an in-progress day. PROVEN ON REAL DATA: the fix
    caught 2026-07-08 00:00:00->00:10:25 (10.4min midnight respawn-delay hole)
    that the buggy version reported as "0 gaps".
  - B2 zero-parseable-records recorded as CLEAN: files present + 0 records (e.g.
    a recv_wall_ns field rename) now records a full-day gap + exits non-zero
    (fail-closed, mirrors --live). No raw files -> B3.
  - B3 re-scanning a raw-PRUNED day WIPED its recorded gaps (raw retention 3d,
    pipeline_supervisor L150): write_record gained replace_day=; the build path
    passes replace_day=False when a day has no raw files, PRESERVING its rows.
    The record now outlives the raw. (--since N>3 previously blanked pruned days
    on every run.)
  - B4 (found re-scanning real data) a corrupt digit-run overflowed the int64
    array and CRASHED the scan: _extract_recv_us now plausibility-windows
    recv_wall_ns to 2020..2100 (D3), dropping+counting corrupt values (07-06 had
    163; scan completes).
- Tests 11 -> 19 (all green): midnight-cross, leading/trailing edge,
  in-progress-day-not-flagged, unreadable fail-closed, pruned-day preserve,
  rescan-after-prune preserve, corrupt/implausible drop. make check + run_pipeline
  PIPELINE PASS; registry 121.
- AUTHORITATIVE RECORD now in work/event_packs/capture_gaps.csv (boundary-aware,
  corrupt-safe): 07-06 14 gaps, 07-07 21 (+2 midnight edges vs 19), 07-08 1 (the
  00:00->00:10 midnight hole; nothing after -> W-C1 holding). 36 total. Every
  backtest over 07-06/07/08 now degrades correctly on overlap.
- PROCESS NOTE for next session: capture_gaps must run DAILY before raw is pruned
  (retention 3d) or a day's gaps are lost forever (B3 preserves what's recorded,
  but can't recover an unscanned pruned day). Wire `capture_gaps --date
  <yesterday>` into the daily export block of pipeline_supervisor.sh (W-C2.1,
  with the --live alert). NON-BLOCKING audit notes accepted: N1 partial-
  subscription loss invisible to a market-wide detector (per-market check later);
  N2/N3 minor.
- STATE: W-C0/C1(+deploy W-C3 partial)/C2 all landed + independently audited +
  remediated. Live pipeline on W-C1 (sha 3db4043), forced=0 + healthy. NEXT: W-C3
  acceptance tail (24h zero-gap: run capture_gaps --date 2026-07-08 after the day
  ends, expect only the pre-deploy 00:00-00:10 hole); wire daily capture_gaps +
  --live alert into the supervisor (W-C2.1). SINGLE-OWNER RULE in force.

## 2026-07-08 00:40 UTC — W-C2 durable capture-gap record + detector (built, real-data-proven)

- commit: e455f99 W-C2 (tools/capture_gaps.py + tests/test_capture_gaps.py +
  tools.json 2 entries + run_pipeline). Independent audit LAUNCHED at exit.
- WHAT: capture_gaps.py builds work/event_packs/capture_gaps.csv (start_us,end_us)
  from raw-feed inter-record silence — the authoritative source for
  event_validate V-EP15 (replaces the coarse quality_log text parser, which stays
  as fallback). --live writes work/live/capture_alert.json + exits 2 on an active
  gap (fail-closed: no raw => gap). Default --min-gap-secs 60 (above W-C1's
  ~20-25s recovery envelope + the 0.33s healthy inter-record max, so normal ops
  yield an EMPTY record). 11 pytest incl. an end-to-end raw-built-record ->
  event_validate=degraded (AF-1).
- KEY BUG CAUGHT MID-BUILD (by running on real data): the first draft streamed
  raw files sequentially by first-record ts and reported 2 PHANTOM gaps on
  2026-07-08 hour-00 — because a within-hour respawn/rotation left two segments
  whose time ranges OVERLAP (file0 00:10-00:32, file1 00:17-00:33). Fix: GLOBAL
  SORT of all record timestamps (int64-us array, memory-cheap) before gap
  detection. Re-run => 0 real gaps for hour-00 (matches forced=0/healthy). A
  regression test pins this. Lesson: raw segments are NOT guaranteed
  chronologically disjoint.
- REAL-DATA RESULT: 2026-07-07 had 19 real capture gaps (31.6M records, 93 files,
  0 unparsed, 22s) — many 45-60+ min: pre-STEP-0 morning 05:24-12:00, and
  post-STEP-0/pre-W-C1 15:28-24:00 incl. the confirmed 20:07:30->21:00 hole. Now
  in the record => backtests over 07-07 correctly `degraded`. CONFIRMS STEP 0
  alone did NOT stop the gaps; only W-C1 (deployed 07-08 00:25) does. 2026-07-08
  post-deploy: 0 gaps so far.
- BACKLOG added: (1) overlapping+oversized rotation segments on 07-08 hour-00
  (possible DUPLICATE records across segments — no data loss, but verify ingest
  dedups + rotation/256MB cap correctness); (2) wire --live alert into the
  supervisor watchdog / cron + operator-chosen email/webhook (W-C2.1).
- NEXT: (1) read the W-C2 independent audit + fix findings; (2) W-C3 acceptance
  tail — a 24h ZERO-gap report post-deploy closes it: run
  `python3 tools/capture_gaps.py --date 2026-07-08` after the day completes
  (expect 0); the live forced-reconnect proof is nice-to-have (feed has stayed
  healthy so the watchdog hasn't needed to fire, forced=0). (3) wire the live
  alert. SINGLE-OWNER RULE in force.

## 2026-07-08 00:25 UTC — W-C1 independent audit CLEARED + DEPLOYED (W-C3, operator go)

- commits: 72aa110 W-C1 audit remediation (atomic last_activity_ms_ + audit-clear).
  Deploy is a binary swap (no new commit): build/ws_shadow rebuilt from HEAD.
- INDEPENDENT AUDIT (3rd agent attempt; first two died on infra API errors)
  CLEARED 06db331 — no blocking defects. Independently confirmed stop()/start()
  restart safety: join() is bounded by ixwebsocket's kClosingMaximumWaitingDelay
  = 300ms (IXWebSocketTransport.cpp:58), so a wedged socket cannot hang teardown.
  Findings: #1 FIXED (last_activity_ms_ was a non-atomic cross-thread race, now
  std::atomic relaxed); #2 RESOLVED by measurement — over a healthy hour the max
  inter-record silence excluding real holes is 0.33s (p99.99 ~0.17s), so the 20s
  force threshold has ~60x margin and can't false-teardown a healthy socket;
  #3 accepted (cold-start wedge -> respawn-bounded, BACKLOG). Full writeup:
  docs/plan_audits/capture_wc1_audit_2026-07-07.md.
- NEW FINDING while resolving #2: hour 23 (07-07) had THREE ~15-min gaps
  (period ~948s). Not restarts (a crash = 15s gap; the 60s supervisor watchdog
  only revives ingest, not ws_shadow) — they are the SAME wedge, recovered late
  by an eventual TCP-level error (~15-min Kalshi LB idle-timeout) instead of at
  :00. So the wedge recovery time is unpredictable (15-60 min); W-C1's 20s forced
  reconnect preempts all of it. Capture was losing ~95% of data in bad hours.
- DEPLOYED (W-C3, operator go/no-go = GO, method: immediate SIGTERM respawn):
  rebuilt build/ws_shadow from HEAD (sha 3db4043, was approved 3c389ae; rollback
  copy at build/ws_shadow.rollback_3c389ae AND scratchpad/ws_shadow.approved).
  SIGTERM'd PID 8428 at 00:25:11Z; supervisor respawned PID 9962 in ~1s. Deploy
  gap ~1-2s (last pre record 00:25:11.9Z, resumed by 00:25:12). CONFIRMED the
  W-C1 binary is live: the stderr counter line now carries `forced=` (old binary
  had no such field): `events=8974 ... reconnects=0 forced=0 errors=0 epoch=1`.
- REMAINING W-C3 TAIL (acceptance not yet fully green): (1) catch the FIRST live
  forced reconnect (forced=>=1 / a `watchdog_reconnect` metrics event) proving the
  watchdog recovers a real wedge in <<1min — a background monitor was started at
  session end (~40min cap); given the ~15-min wedge cadence it should fire soon.
  (2) W-C2 durable structured capture-gap detector + 24h zero-gap report. Rollback
  if it misbehaves: cp build/ws_shadow.rollback_3c389ae build/ws_shadow && SIGTERM.
- NEXT: confirm the live forced-reconnect proof; then W-C2 (gap detector+alert);
  then the 24h zero-gap report closes W-C3. SINGLE-OWNER RULE in force.

## 2026-07-07 21:20 UTC — W-C0 diagnosis + W-C1 force-reconnect watchdog (built, tested, NOT deployed)

- commits: 8f89d88 W-C0 capture diagnosis; 06db331 W-C1 force-reconnect watchdog
  + red-first test. (Audit + decision docs staged this session — see below.)
- ROOT CAUSE FOUND (W-C0, lives in docs/plan_audits/capture_diagnosis_2026-07-07.md):
  the top-of-hour gaps are a **silently-wedged (half-open) socket**. It delivers
  no Close/Error, so ixwebsocket never attempts a reconnect; STEP 0's re-sign only
  runs ON a reconnect attempt, so it never runs; the ping-silence watchdog detects
  the 30s silence but only did `++missed_pong_disconnects` — never forced recovery.
  Dead until the :00 respawn. Contributing mechanism: `ix_transport.cpp`
  `setPingInterval(0)` disables ixwebsocket's own heartbeat, so the library cannot
  detect the dead socket itself. STEP 0 (6d56aae) IS in the running binary and is
  NOT the bug (it fixes a different mode: 401 relockout on reconnect).
- The four W-C0 questions answered with cited evidence; caught a LIVE 46-min gap in
  progress (07-07 20:07:30 -> 21:00 UTC recovery). Q1 CAPTURE (raw firehose frozen,
  not ingest lag), Q2 right binary, Q3 reconnect flat at 0 across the gap, Q4
  watchdog only counts.
- FIX (W-C1, decision in code + docs/plan_audits/capture_wc1_audit_2026-07-07.md):
  KalshiWsClient::force_reconnect() = t_.stop() + refresh_auth() + t_.start();
  wired into the ws_shadow watchdog — after **20s** of zero inbound frames it
  forces the reconnect (bounded **15s** backoff, logged, `watchdog_reconnect`
  metrics event, `forced=` on the counter line). Recovery: up-to-60min -> ~20-25s.
  Healthy path byte-for-byte unchanged (force is unreachable unless the whole
  all-markets firehose is silent 20s). Fail-closed (S2).
- red-first PROVEN: neuter force_reconnect -> recovery asserts FAIL; restore ->
  make check GREEN + tests/run_pipeline.sh PIPELINE PASS (ws_shadow_mock ran the
  REAL W-C1 binary). Test at tail of tests/test_ws_client.cpp.
- AUDIT: independent fresh-context agent FAILED TWICE on an infra API error
  ("Connection closed mid-response"); a rigorous in-context self-audit traced the
  vendored ixwebsocket source and found NO BLOCKING DEFECTS (stop() close()s+wakes
  the timeout-bounded poll then joins => wedged socket exits, no hang; start()
  after join spawns a fresh thread => restart supported; join is a barrier => no
  new race). 3 non-blocking notes (20s threshold tunable to 30-45s; legacy
  missed_pong may undercount; 2 weak test asserts). Full writeup in the wc1_audit
  doc. **A 3rd independent-agent audit was relaunched at session end.**
- NOT DEPLOYED (critical): build/ws_shadow is RESTORED to the approved pre-W-C1
  binary (sha 3c389ae; W-C1 binary was sha 560fc23). The live pipeline (PID at
  21:00 respawn) runs the approved binary. run_pipeline.sh relinks build/ws_shadow
  via run_ws_shadow_mock.sh (`make build/ws_shadow`), so ALWAYS back up + restore
  the approved binary around it until W-C3, or the next hourly respawn deploys
  unaudited code. Backup kept at scratchpad/ws_shadow.approved this session.
- DECISION for the operator's "≤1s seamless recovery" ask (in docs/BACKLOG.md):
  a single socket CANNOT hit 1s (detection >~0.5-1s + reopen 1-3s). Sub-second
  continuity = REDUNDANCY (dual independent WS feeds, hot standby, de-dup at the
  book layer) and belongs to the **Phase-2 EXECUTION feed** (World A/B merge, Q5),
  NOT this capture path. W-C1 makes the CAPTURE/backtest feed complete; dual-feed
  is a separate Phase-2 W.
- NEXT SESSION: (1) read the 3rd independent audit result (agent was running at
  exit) and fix anything it finds; (2) W-C2 (durable structured capture-gap record
  + alert, replaces the coarse quality_log parser as V-EP15's source); (3) W-C3
  deploy is operator go/no-go, gated on independent audit clear + zero-gap 24h
  proof. SINGLE-OWNER RULE in force.

## 2026-07-07 20:15 UTC — 🔴 CAPTURE STILL DROPPING TODAY (post-fix) — next session #1 priority

- HOW THIS SURFACED: operator asked to pull real sports games + graph them.
  Pulling today's matches (Pegula vs Gauff, Sinner vs Struff, Vrancken vs
  Ruggeri) exposed that CAPTURE STILL HAS GAPS on 2026-07-07, INCLUDING AFTER
  the STEP 0 fix supposedly deployed ~13:00 UTC.
- EVIDENCE (market-wide silent windows — the WHOLE Sports firehose goes dark,
  every gap recovers exactly at the top of the hour = the 401/reconnect
  signature, same as the original bug):
  - 07-06: 12:26–13:00 (33m), 13:39–14:00 (20m)  [de Minaur vs Cobolli match]
  - 07-07 pre-fix morning: ~05:24, 06:13, 07:02, 08:05, 09:00, 10:11–11:00 ...
  - 07-07 POST-fix (the alarming part): 13:39–14:00 (20m), 14:48–15:00 (12m) —
    both recover at the top of the hour.
- SO: STEP 0 (WS re-sign on reconnect, commits 6d56aae + deploy) is NOT
  confirmed to actually stop the gaps. The CURRENT ws_shadow (hour 20) shows
  reconnects=0 errors=0, but earlier hours today still dropped. My mid-session
  claim "the fix is holding" was PREMATURE — retract it.
- HYPOTHESES to test next session (do NOT build features on top — fix this
  first): (1) is the fixed ws_shadow binary ACTUALLY the one running each hour,
  or did a respawn load an old binary? (2) does STEP 0 recover a mid-hour drop
  at all, or does the socket stay dead until the :00 hourly respawn regardless?
  (the top-of-hour recovery pattern strongly implies reconnect is NOT working
  and only the hourly restart revives it). (3) the AF-5 residual: at max backoff
  the re-signed timestamp can be ~30s stale (BACKLOG) — but that would not cause
  20-min gaps; a 20-min gap means reconnect is failing outright until :00.
  (4) is it capture (raw) or ingest-lag (staging)? These gaps were read from
  STAGING; cross-check against raw work/raw/date=2026-07-07/firehose_*.ndjson
  inter-record gaps + work/metrics.ndjson freshness to confirm it is CAPTURE,
  not ingest tailing behind.
- WHAT IS FINE (do not re-litigate): the captured VALUES are byte-exact correct
  (0 crossed books, prices match Kalshi results). V-EP15 flags gap-overlapping
  events `degraded` so they will NOT silently feed a backtest — the safety net
  works. The problem is purely CAPTURE COMPLETENESS (holes), which must be fixed
  before the data is trustworthy for the minutes that matter (settlement-
  convergence run is highest-vol and often lands in a hole).
- artifacts produced this session (operator-facing, not committed — under
  work/event_exports/, gitignored): per-event trades.csv + orderbooks_l1.csv
  (labeled, E4-exact, NO side derived = 1 − YES) for KXATPMATCH-26JUL06DECOB
  and the 3 today matches; two claude.ai chart artifacts (single-match full-res;
  3-today-games with red capture-gap bands).
- NEXT SESSION #1: harden capture. Confirm STEP 0 is really running + effective;
  find why top-of-hour gaps persist; add a durable structured capture-gap record
  (BACKLOG) so V-EP15 reliably catches these. Everything else (W-E4/E5/W-LC,
  settlements-join) waits. SINGLE-OWNER RULE still in force.

## 2026-07-07 18:55 UTC — W-E1 real-dim wiring DONE: index builds on the LIVE warehouse + audit-fixed (2 defects)

- commits: b5b6228 --from-warehouse builder; 32a8ae4 audit remediation (NULL identity, empty build)
- decisions (in files):
  - event_index --from-warehouse (tools/event_index.py build_index_from_warehouse
    + _observed_by_market + _dim_markets): builds the index natively from the REAL
    warehouse + dim/latest, not a synthetic catalog dir. Observed activity
    aggregated in SQL over trades∪L1 (never materializes ticks); category/series/
    group from observed rows; open/close/status/mve join from dim by (event,ticker);
    lifecycle Fork-B. Reuses the tested infer_index_row core.
  - PROVEN ON LIVE DATA: 225,936 units (32,336 cross-midnight; 182,360 Q7-excluded
    MVE). Most units are catalog_incomplete + observed_merged because dim/latest
    holds only CURRENT markets (settled events dropped) — correct fail-closed.
- INDEPENDENT AUDIT: 2 CONFIRMED defects, both FIXED (32a8ae4):
  - NULL event/market identity → bogus unit + None-vs-str sort crash → filtered
    fail-closed. Empty build → executemany([]) crash → guarded (empty parquet).
  - Audit CLEAN: row-count not money-double-count, padding property, market-unit
    isolation, SQL escaping.
- KNOWN LIMITS (BACKLOGged, not defects):
  - dim/latest lacks settled markets → join catalog/settlements for accurate
    settled-game windows (W-E1.1 / STEP 5 settlements-first).
  - research reads (event_pack/export/validate → wh.load) can hit the LIVE ingest
    daemon's staging write lock (DuckDB single-writer, D6); load()'s 12s attach
    retry can be too short during a long ingest window. The real end-to-end export
    (index→pack→validate→CSV) is proven on synthetic (W-E7 tests) but was blocked
    on live data by this lock at the time; the real INDEX build succeeded.
- verification: make check GREEN; run_pipeline PIPELINE PASS; 12 event_index tests.
- STATUS: event-packaging W-E0/E1(+real-dim)/E2/E3/E7 all DONE + audited. The
  operator can select any market/event/series → clean complete separated CSVs,
  and the index now builds on real production data. Remaining: settlements-join
  (accurate settled windows), staging-lock hardening for research reads, W-E4
  (backtest wiring), W-E5/W-LC. SINGLE-OWNER RULE in force.

## 2026-07-07 18:30 UTC — W-E7 DONE: operator-facing per-event CSV export (the deliverable) + audit PASS

- commits: b18d260 W-E7 event_export.py + tests; 3bd016a audit hardening (dest path sanitize)
- decisions (in files):
  - tools/event_export.py: three-axis selector (--event | --market | --series)
    → clean per-event CSV folders under work/event_exports/<series>/<event>/
    (trades.csv, orderbooks_l1.csv, _event_summary.csv, _manifest.json). COMPOSES
    W-E2 packer + W-E3 validator — never re-derives money, so E4 fixed-point is
    byte-exact (test: sub-penny e4=90 stays "90"). _manifest.completeness =
    validator verdict; `pass` ONLY if event_validate passes, interior gap ⇒
    `degraded` (V-EP15/AF-1), never a silent green CSV. --market filters to the
    single market; no mixing events in one file.
- INDEPENDENT AUDIT: VERDICT PASS (0 blocking). New surface only (W-E2/E3 already
  audited). Confirmed clean: money round-trips byte-exact through csv reader/
  writer (no float), --market filter column-correct for both tables, completeness
  never rewritten pass, non-packed units surface skipped/refused (no crash), SQL
  escaped. 2 LOW notes: (1) dest path separators unsanitized → FIXED (3bd016a);
  (2) --market on an event ticker → 0-row export (operator misuse, row_counts=0
  signals it) → left as-is.
- verification: make check GREEN; run_pipeline PIPELINE PASS; registry 119; 4
  W-E7 tests green.
- STATUS — event-packaging plan: W-E0/E1/E2/E3/E7 DONE (each independently
  audited, defects fixed). The operator can now select any market/event/series
  and get clean, complete, separated CSVs — the original 2026-07-07 requirement
  is MET. Remaining (optional/gated): W-E4 (wire axes into mm_backtest/gate_calc),
  W-E5 (daily pack job), W-LC (lifecycle capture, operator-gated). Real-dim
  wiring for the index (W-E1 runs on catalog-dir/fixtures) + a durable structured
  capture-gap record are BACKLOGged. SINGLE-OWNER RULE in force.

## 2026-07-07 18:05 UTC — W-E3 DONE: event_validate (V-EP + V-EP15/AF-1) + load(event=); audit PASS-after-fix (5 defects)

- commits:
  - 4f48407 W-E3: event_validate.py (V-EP checks) + warehouse.load(event=) + schema §Event packs
  - 5ea341a W-E3 audit remediation (5 defects; V-EP15 was inert in production)
  - (BACKLOG: durable structured gap-record builder)
- decisions (in files):
  - warehouse.load(event=KEY, index_path=) resolves the unit's window + market
    set from the W-E1 index and returns exactly that episode across day
    partitions (three-axis selector §3.5); day-mode unchanged. → tools/warehouse.py.
  - event_validate.py stamps pass|degraded|fail. Implemented V-EP1/3/4/6/7/10/12
    + V-EP15 (AF-1 interior-gap: window∩capture-gap ⇒ degraded, never pass).
    Deferred checks (V-EP5/8/9/11/13/14) emitted as explicit skip (D2).
    → tools/event_validate.py; schema §Event packs (E5).
  - AF-1 OBLIGATION DISCHARGED: interior_gap red fixture built + proven RED
    (neuter V-EP15 ⇒ holed pack stamped pass; restored ⇒ degraded).
- INDEPENDENT AUDIT of W-E3 found 5 CONFIRMED defects, all FIXED (5ea341a):
  - D1 HIGH: V-EP15 was INERT in production — defaulted to a nonexistent
    capture_gaps.csv; missing record ⇒ gaps=[] ⇒ pass. Fixed: source is
    quality_log.ndjson (gaps_from_quality_log), FAIL-CLOSED when unavailable
    (V-EP15 skip ⇒ degraded, never pass).
  - D2 MED: deferred checks silently omitted (contradicted the D2 claim) → now
    explicit skip.
  - D3 MED: --refresh updated manifest window but not index.parquet → load(event=)
    truncated; event_pack now writes re-inferred win_end back to the index
    (atomic, preserves markets[]).
  - D4/D5 LOW: null-window TypeError + empty-markets IN () ParserException → now
    fail-closed. Audit CLEAN on day-mode regression, V-EP1 self-consistency,
    money (D5, no float), SQL escaping.
- verification: make check GREEN; run_pipeline PIPELINE PASS; registry 117
  tools; 22 event-packaging pytest cases green.
- blocked / handoff: event-packaging plan status — W-E0/E1/E2/E3 DONE (each
  audited). Remaining: W-E4 (three-axis wiring into mm_backtest/gate_calc),
  W-E7 (CSV export — the operator-facing deliverable), W-E5 (daily pack job),
  W-LC (lifecycle capture, operator-gated). V-EP15's gap source is a coarse
  best-effort parser (fails closed) — a durable structured gap-record builder
  is BACKLOGged. SINGLE-OWNER RULE still in force.

## 2026-07-07 17:35 UTC — Independent audit of AF-1..4 remediation: 1 real DEFECT found + fixed (AF-3 was incomplete)

- commit: 89a6b98 AF-3 residual — per-partition staging dedup for aggregate queries
- WHY: the brief's EXIT required an independent audit of the AF-1..4 remediation
  before DONE. It ran (fresh context) and CAUGHT A REAL DEFECT in my AF-3 fix
  (902db78): I only scoped the GLOB, which fixed pinned category+subcategory
  queries but left the identical undercount on AGGREGATE queries — category=None
  (exactly what `event_measure_split --category all` and every all-category
  backtest tape use) and subcategory=None still applied a scalar global max to
  all staging rows. Repro: category=None → 4, expected 6. My schema doc + §6
  self-audit had WRONGLY claimed it closed (a D2/Q10 "green lies" miss — the
  auditor flagged exactly this).
- FIX (proper this time): _archive_files returns a per-(sani_cat,sani_sub) max-
  date map; load() anti-joins staging against it (keep row iff its own partition
  unarchived OR ts past that partition's cutoff), matching staging (cat,sub) to
  the sanitized archive dir names via a SQL replica of warehouse_common.sanitize
  (sanitize is not invertible; verified real dirs are sanitized, e.g.
  "Aussie Rules"→"Aussie_Rules", "_none"→"none"). Red-first category=None case.
- audit's other findings: AF-1/AF-2/AF-4 confirmed SOUND; all red-first tests
  genuine; AF-2 money math clean (HUGEINT notional is load-bearing vs int64
  overflow, no float); AF-1 V-EP15 conceptually closes the interior-gap hole but
  the interior_gap red fixture is a PROMISSORY NOTE (must be built RED when W-E3
  lands — not satisfied yet, only specified).
- verification: full warehouse-consumer suite (gold/ingest/export/coverage/
  event/warehouse_status, 199 pytest + 3 self-running) GREEN — no regression on
  the shared read path. make check GREEN; run_pipeline PIPELINE PASS.
- blocked / handoff: AF-1..AF-4 brief now genuinely DONE (all 4 confirmed +
  remediated, remediation independently audited, the one found defect fixed).
  W-E3 (event_validate.py) MUST implement V-EP15 + build the interior_gap red
  fixture RED (the outstanding promissory note). SINGLE-OWNER RULE still in force.

## 2026-07-07 17:05 UTC — AF-1..AF-4 money-integrity brief: all 4 CONFIRMED + red-first remediated

- commits (one per item, revertable independently):
  - 4284597 AF-1 plan V-EP15 (interior-gap completeness) + brief verbatim
  - 902db78 AF-3 warehouse.load() per-category staging exclusion + red-first test
  - 3207bd5 AF-2 contract/notional-weighted split headline + reorder test
  - 240af42 AF-4 collect() SQL-path end-to-end test (+ warehouse= passthrough)
  - (BACKLOG: AF-2 L1-quote-window deferral)
- DISPOSITIONS (all re-derived from file:line; brief was a LEAD, verbatim in
  docs/plan_audits/event_packaging_af1-4_brief_2026-07-07.md):
  - AF-1 CONFIRMED — PLAN §6 V-EP1/2/10/12 are day-granular / pack↔warehouse
    self-consistency; none joins the capture-gap record → an interior sub-day
    outage passes while the mid-game (settlement-convergence, highest vol) is
    missing → a green pack feeds a Q2 bound on holed data. Fix = SPEC ONLY
    (event_validate is W-E3, unbuilt): V-EP15 interior-gap check vs quality_log
    data_loss/STALE spans → completeness=`degraded` (never pass), gaps in
    manifest; `interior_gap` red-fixture row; degraded state defined. → PLAN §6.
  - AF-2 CONFIRMED — collect() summed count(*) only. Added Σcount_e4 (contracts)
    + Σ HUGEINT(price_e4)·count_e4 (notional_e8), integer throughout (D5, no
    float on money). NEW WEIGHTED HEADLINE (Sports 7d): cross-midnight =
    40.7% events / 76.3% trade-rows / **82.1% contracts** / 78.2% notional —
    the row metric UNDERSTATED the $ split. → tools/event_measure_split.py.
  - AF-3 CONFIRMED — _archive_files computed max_arch_date from a GLOBAL `*/*`
    glob; a category archived through a later day than another silently dropped
    the lagging category's not-yet-archived staging rows (undercount on EVERY
    backtest tape → Q2). Scoped the date-set glob to the queried category/
    subcategory. → tools/warehouse.py + schema §Access (E5).
  - AF-4 CONFIRMED — tests exercised event_spans (pure) only; collect()'s SQL
    (ts_utc//US_PER_DAY, sum, HUGEINT) was unverified. Added a fixture-warehouse
    end-to-end test + warehouse= passthrough. → tests + tool.
  - REFUTED: none (all four held).
- red-first (anti-fake-green D2): AF-3 Crypto=2 RED before / 4 after; AF-2
  reorder FAILS with weights zeroed; AF-4 FAILS with a corrupted // divisor.
  All GREEN after fix. AF-1 is spec-only (no code path to redden yet — V-EP15
  redness is W-E3's obligation, pinned as the interior_gap fixture).
- GUARDRAILS §6 self-audit of this remediation: (1) phase 1.5, no gate skip ✓
  (2) no live orders ✓ (3) no strategy math; notional is an integer weight ✓
  (4) doesn't compute a Q2 bound — it CLEANS the data feeding it ✓ (5) WS-capture
  warehouse ✓ (6) every fix ships a red-first test ✓ (7) capture/ingest/export
  untouched; only a load() READ-path fix + read-only tools, all existing
  warehouse/gold/ingest/export tests green ✓ (8) each item its own commit,
  ≤300 LOC, AF-1 reverts cleanly ✓ (9) schema §Access + PLAN §6 updated (E5) ✓
  (10) explicitly de-lies: V-EP15, honest weighted labels, no-undercount,
  tested number ✓.
- context capsule: make check GREEN; run_pipeline PIPELINE PASS (registry 114
  tools). event_measure_split CSV columns changed: total_trade_rows +
  total_contracts_e4 + total_notional_e8 (was total_ticks). collect() + main
  gained warehouse=/--warehouse.
- blocked / handoff: brief's EXIT calls for an INDEPENDENT audit of THIS
  remediation vs §6 before DONE — not yet run (next step). Then resume the plan
  at W-E3 (event_validate.py must IMPLEMENT V-EP15 + the interior_gap red
  fixture). SINGLE-OWNER RULE still in force.

## 2026-07-07 16:45 UTC — W-E2 DONE: event pack materializer (money-integrity enforced) + audit PASS-after-fix

- commits:
  - 8c59da9 W-E2: tools/event_pack.py + tests/test_event_pack.py (6) +
    tools.json/run_pipeline registration
  - 8acab89 W-E2 audit remediation (2 MAJOR: AF-5 boundary, L1 idempotency)
- decisions (in files):
  - event_pack materializes per-unit packs work/event_packs/data/unit=<key>/
    {trades,orderbooks_l1}.csv + manifests/<key>.json from a W-E1 index row,
    read-only via wh.load(). MONEY-INTEGRITY enforced as code+tests: E4 integer
    columns selected VERBATIM (tools/event_pack.py _SRC), written byte-exact via
    csv.writer — zero float surface, no dollar-string derivation (AF-1/AF-2).
    count_e4 = contract qty; row_counts = cardinality (AF-3).
  - AF-5 stale-window: sealed-only + pack-time observed_last_ts re-check;
    REFUSE (fail-closed) if activity >= win_end, or --refresh re-infers
    (win_end = obs_last + 1, exclusive-end aware). → tools/event_pack.py.
- context capsule:
  - Independent W-E2 audit VERDICT: money-integrity CORE CLEAN (probed duckdb
    fetchall: INTEGER/BIGINT→int, no Decimal/float path; sub-penny e4=90 stays
    "90"; NULL→empty; archive/staging dedup inherited via load() max_arch_date;
    refuse/skip have no side effects). Found 2 MAJOR CONFIRMED, both FIXED in
    8acab89:
    * Defect-1: guard `obs_last > win_end` inclusive vs extract `ts_utc <
      win_end` exclusive → tick AT win_end silently clipped (reachable with
      post_pad_us:0). Fixed to `>=`.
    * Defect-2: L1 ORDER BY lacked a unique tiebreak (record_class constant per
      market) → same-µs rows reorder under DuckDB parallel sort → nondeterministic
      manifest md5 (breaks §3.3 idempotency). Fixed: order by all payload cols.
      Regression tests added (same-µs L1 determinism; exact-win_end refuse).
  - Pack test harness builds a synthetic 2-day archive (trades csv.gz + L1
    parquet) — reusable pattern for W-E3.
- blocked / handoff:
  - Next W = **W-E3** (event_validate.py V-EP* + warehouse.load(event=...)
    integration). The AF-1..AF-5 findings doc (docs/plan_audits/) is the
    checklist; V-EP checks should assert the money-integrity + no-clip
    guarantees W-E2 now provides.
  - SINGLE-OWNER RULE still in force (do not relaunch the parallel process).
  - Fixture-only so far: W-E1 index + W-E2 packs run on synthetic catalog/
    warehouse; real dim/latest wiring still pending (W-E1.1 or W-E3 prereq).

## 2026-07-07 16:10 UTC — Consolidation: single-owner restored; money-integrity audit recorded (AF-1..AF-5) + plan spec fixed

- commits:
  - 0c6d95d money-integrity audit doc + PLAN_EVENT_PACKAGING §3.6/W-E2 fixes +
    W-E0 label (AF-4)
- WHY THIS ENTRY: a PARALLEL process had been executing/committing the event-
  packaging Ws on this branch (it authored PLAN_EVENT_PACKAGING, rewrote the
  W-E1 fixtures/test under me, and committed 37523e2/771b2de incl. this
  session-log block). Two writers on one branch = clobber risk (hit once: a
  Write rejected mid-edit, a commit found nothing to stage). Operator HALTED the
  parallel process and put this session in sole charge. Branch confirmed
  quiescent (head stayed 771b2de through the audit).
- decisions / findings (now IN A FILE, closing the E2 gap):
  - The parallel process referenced a "W-E0 money-integrity audit, AF-1..AF-4,
    fix plan spec before W-E3" — but those findings were NEVER written anywhere
    (only a one-line SESSION_LOG mention) and are unrecoverable. Superseded by a
    fresh independent audit → docs/plan_audits/event_packaging_money_integrity_
    2026-07-07.md (AF-1..AF-5, verbatim).
  - AF-1/AF-2 (BLOCKER, D5): the §3.6 CSV contract would have floated money.
    export_day.STRATEGY_COLS emits yes_price_e4/10000.0 as FLOAT and drops the
    _e4 columns → sub-penny loss (0.0090 -> 0.01). Plan §3.6 now MANDATES E4
    integer columns byte-exact + integer-4dp dollar strings, forbids float/2dp,
    and forbids reusing STRATEGY_COLS. Gates W-E2/W-E7.
  - AF-3: count_e4 (contract qty) vs row_count/n_trades disambiguated in §3.6.
  - AF-4: W-E0 tool prints an honesty note ('ticks' = trade ROWS, not contract
    volume); plan _event_summary relabeled. (W-E0 corrupts no money — it never
    loads count_e4 — this is labeling only.)
  - AF-5 (correctness): a stale index row can clip late trades at pack time.
    W-E2 acceptance now requires pack-time window RE-INFERENCE + sealed-only
    packing + an anti-clip regression case.
- context capsule:
  - Audited base state: W-E0 (d3791d5,a74ed93) + W-E1 (37523e2) both verified
    green independently (14/14 event tests, pipeline PASS, registry 111 tools).
    W-E1 carries NO money path (audit CLEAN). The money risk is entirely in the
    NOT-YET-BUILT W-E2/W-E7 CSV materializers, now spec-guarded.
  - No W-E2 code written yet.
- blocked / handoff:
  - Next W = **W-E2 (pack materializer)** — build to the AMENDED §3.6 money-
    integrity contract + AF-5 pack-time-reinference acceptance. This is where
    the BLOCKER guards get enforced by tests (anti-float assert; late-trade
    anti-clip case).
  - SINGLE-OWNER RULE: do not re-launch the parallel event-packaging process;
    one writer on this branch.

## 2026-07-07 15:30 UTC — W-E1 DONE: event index builder + fixture tests green

- commits:
  - 37523e2 W-E1: `tools/event_index.py` — §3.2 A–G window inference, §3.4 parquet
    schema; fixture catalog + 9 pytest cases; tools.json + run_pipeline registration
- decisions:
  - W-E1 reads synthetic catalog dir CSVs only (markets/observed/lifecycle); does
    NOT extend warehouse.py (W-E3 owns `load(event=...)`). Derived output:
    `work/event_packs/index.parquet`.
  - Seal-state "recent ticks" gate tightened: `active` requires
    `now - seal_after <= t_last <= now` (future observed ticks no longer force
    active). Lives in `tools/event_index.py::_seal_state`.
  - Dim contract confirmed for later wiring: `dim/latest/markets.csv` uses
    `ticker` (not `market_ticker`), `open_time`/`close_time` as
    `YYYY-MM-DD HH:MM:SS` UTC, `mve_collection_ticker` for Q7.
- context capsule:
  - `infer_index_row()` implements §3.2 B–G; `build_index()` does A (event vs
    market unit from `config/event_packaging.yaml`). Window end priority:
    determined → settled → last_seen → scheduled_close; `observed_merged` when
    only ticks. Divergence flag: >2h between t_last/sched_close or
    sched_start/t_first.
  - Fixture run (`--now 2026-07-07 01:00:00`): 8 units — active 2, excluded 2
    (Exotics + KXMVE), partial 2, sealed 1 (KX-LC all settled), scheduled 1.
    Cross-midnight: KX-SPORT-EV1 only.
  - `make check` green; `./tests/run_pytest.sh tests/test_event_index.py` 9/9.
  - AF-1–AF-4 from W-E0 money-integrity audit still open (fix plan spec before
    W-E3; not remediated this session).
- blocked / handoff:
  - Next W = **W-E2** (pack materializer) per PLAN_EVENT_PACKAGING.
  - W-E1 not yet wired to real `dim/latest` — catalog-dir CLI is the Fork B
    interface until a dim-export helper lands (optional W-E1.1 or W-E2 prereq).

## 2026-07-07 13:53 UTC — Event-packaging plan committed + slotted; W-E0 DONE + audit PASS

- commits:
  - a96024a PLAN_EVENT_PACKAGING (per-event data marts; was untracked on disk —
    committed verbatim per full-text-preservation rule)
  - a3.. MASTER_SEQUENCE amendment: slot PLAN_EVENT_PACKAGING before STEP 5
  - d3791d5 W-E0: config/event_packaging.yaml + tools/event_measure_split.py +
    test + fixture + tools.json/run_pipeline registration
  - a74ed93 W-E0 audit remediation (real per-event category in CSV; gap-day test)
  - (BACKLOG appended: deferred W-E0 audit items)
- decisions:
  - Per-event packaging is a DERIVED layer on the warehouse, NOT a re-capture:
    every row already carries event_ticker/market_ticker/ts_utc; raw+archive stay
    day/hour-partitioned (D1). Design lives in docs/PLAN_EVENT_PACKAGING.md
    (three-axis market|event|series selector; per-event CSV bundles with
    completeness manifests; windows from observed activity + lifecycle Fork A/B,
    NEVER the unreliable scheduled close_time).
  - IMPORTANT provenance note: docs/PLAN_EVENT_PACKAGING.md already existed on
    disk (untracked, ~30KB) when this session went to write it — NOT authored by
    this session. It was read in full, judged guardrail-aligned + matching the
    operator requirement, operator-confirmed, then committed verbatim. My
    independent design converged on the same architecture (validation).
  - Category→packaging-unit policy lives ONLY in config/event_packaging.yaml (E2).
- context capsule:
  - W-E0 headline (real data, Sports, warehouse day 2026-07-06 + staging 07-07):
    42.8% of events (1353/3159) cross a UTC-midnight boundary and account for
    77.6% of TRADE ROWS / ticks (count(*), NOT contract volume — the trades
    `count` field is not summed). Example: KXMLBGAME-26JUL062210COLLAD had 1,348
    trade rows on day-06 vs 70,152 on day-07 → a `--date 2026-07-06` backtest
    sees ~1.9% of that game. Report tool: tools/event_measure_split.py →
    work/event_packs/split_report_<date>.csv (derived, gitignored).
  - Independent audit of W-E0: VERDICT PASS. Confirmed clean: day-index boundary
    math (µs, US_PER_DAY exact), event_spans multi-day/gap-day aggregation,
    load() archive/staging double-count guard (max_arch_date filter — verified
    no overlap, uniform archive to 2026-07-06), guardrails (read-only, P4, E3
    registry valid at 109 tools, no float-narrowing). Two MINORs fixed in
    a74ed93 (CSV real per-event category; gap-day test). Two deferred to BACKLOG:
    collect() SQL path not unit-tested (fold into W-E1); pre-existing load()
    global-max-archive-date guard could undercount if categories archive
    non-uniformly.
  - NEXT W = W-E1 (event index builder, tools/event_index.py): implements
    PLAN_EVENT_PACKAGING §3.2 window inference A–F + writes work/event_packs/
    index.parquet; consumes config/event_packaging.yaml. Read-only derived,
    executable now. §3.2/§3.4 in the plan are the spec.
- blocked / handoff: none blocking. Master-sequence order: STEP 1 (AWS migration)
  is still the top-level next STEP; PLAN_EVENT_PACKAGING (W-E1→E7, W-LC gated) is
  slotted before STEP 5 and its W-E1 is runnable whenever the operator wants to
  continue it. STEP 0 remains deployed/live.

## 2026-07-07 13:19 UTC — STEP 0 DEPLOYED to Mac pipeline (Option A) + capture-continuity measured

- commits: (this commit) docs/BACKLOG.md (capture-continuity finding) + this
  entry. No code change — deployment + measurement only.
- what happened:
  - The fixed ws_shadow binary is LIVE in production. The 13:00:01 UTC hourly
    respawn (PID 58944) loaded the on-disk build/ws_shadow that already carried
    the STEP 0 fix (built 12:27 UTC during verification), so the 401-lockout
    exposure has been CLOSED since 13:00:01 UTC. A clean rebuild from committed
    HEAD (3bdb092) is now staged on disk (13:18 UTC) and auto-loads at the next
    hourly boundary (14:00 UTC). build/ws_shadow.prev is a backup (note: it is
    ALSO a fix-containing binary; the true pre-fix rollback is
    `git checkout 72665c5 -- src include && make build/ws_shadow`).
  - Deploy method = Option A (no manual kill): the launchd supervisor
    (com.ritcardo.kalshi-pipeline) respawns ws_shadow each UTC hour, so the
    rebuilt binary deploys on the natural boundary with a ~0.5s handoff and no
    mid-hour shard-collision hazard.
  - Current session verified healthy: events climbing, capture shard
    firehose_13.ndjson.1 growing ~1MB/3s, reconnects=0 errors=0 epoch=1.
- MEASURED capture continuity (work/raw/date=2026-07-07 first/last recv_wall_ns
  per hour) — answers the operator's discontinuity concern with data:
  - Pure hourly restart handoff = ~0.5s (00->01 0.46s, 04->05 0.94s,
    12->13 0.54s). NOT a strategy problem.
  - The 401 bug WAS the damage: hours 06-11 each captured ~30s then went dark
    for the rest of the hour (2900-3800s holes). STEP 0 converts a mid-hour
    disconnect from "dead till next hour" into a seconds-scale reconnect blip.
  - Residual continuity holes = SYNCHRONOUS export in the capture loop: ~8min
    gap at 02:00 UTC (second-pass --force sweep) + ~78s at 00:00 UTC (daily
    export). Logged to docs/BACKLOG.md with fix direction (decouple Layer-3
    export from Layer-1 capture). This is the real remaining 硬伤 for live
    sports capture; STEP 1 (AWS migration) is a natural place to fix it.
- blocked / handoff: STEP 0 fully done + deployed. HEAD-built binary loads at
  14:00 UTC (verify: new PID != 58944 started ~14:00, log healthy). Next W is
  STEP 1 — AWS FULL MIGRATION (draft docs/PLAN_AWS_MIGRATION.md). Consider
  folding the export/capture decoupling (BACKLOG) into it.

## 2026-07-07 12:36 UTC — STEP 0 DONE + independent audit PASS — WS reconnect re-signs auth (401-lockout fix)

- commits:
  - 6d56aae STEP 0: re-sign WS auth headers before every reconnect (fix 401
    lockout) — src/ws_client.cpp + include/kalshi/ws_client.hpp +
    include/kalshi/ws_transport.hpp (mock inject_error) + tests/test_ws_client.cpp
  - d29c7e2 BACKLOG: audit residual (reconnect re-sign can be up to 30s stale
    at the backoff cap)
  - (quality_log entry appended to work/quality_log.ndjson — git-IGNORED, so it
    is NOT in a commit; it lives on-disk as the ops record. wp=STEP0-ws-resign,
    window 2026-07-07T06:00-09:00Z.)
- decisions (each in the named file):
  - Fix = client re-signs fresh handshake headers before every connection
    attempt via KalshiWsClient::refresh_auth(), called on transport Close AND
    on handshake Error → src/ws_client.cpp (start/on_close/on_message Error).
    Rationale: a 401 handshake rejection emits Error with NO Open/Close
    (ixwebsocket IXWebSocket.cpp:362), so re-signing only on Close would miss
    the 401-loop path entirely. ixwebsocket re-reads _extraHeaders each
    connect() on its own background thread; the synchronous re-sign runs to
    completion on that same thread before the next attempt.
  - Injectable Clock (KalshiWsClient::set_clock) added so the red-first test
    proves a strictly NEWER timestamp deterministically (no sleeps) →
    include/kalshi/ws_client.hpp + tests/test_ws_client.cpp.
  - Residual (max-backoff staleness, undocumented Kalshi auth window) is NOT
    fixed this session; logged with two follow-ups → docs/BACKLOG.md.
- context capsule (brief your replacement):
  - ROOT CAUSE (proven): headers signed once at startup; ixwebsocket
    auto-reconnect replays the stored KALSHI-ACCESS-SIGNATURE/TIMESTAMP → stale
    → Kalshi 401s every reconnect until the hourly process restart re-signs.
    Clock exonerated: sntp 46ms + REST auth passing during the 06:00-09:00 UTC
    outage. (facts: clock_skew_ms_vs_exchange=1138, preflight ±2000ms — that is
    LOCAL skew, NOT the server-side signature-timestamp window, which is
    UNDOCUMENTED anywhere in the vendor snapshot.)
  - VERIFICATION: red-first test_ws_client (+24 checks). With the two
    refresh_auth() calls removed, exactly 3 assertions FAIL (newer-ts-on-close,
    signature-re-signed, newer-ts-on-401); restored → ALL PASS. make check
    GREEN; run_pipeline.sh PIPELINE PASS (ws_shadow_mock pass, test_ws_client
    +24); production apps ws_shadow + ws_smoke build clean (no warnings).
  - INDEPENDENT AUDIT (fresh-context agent, adversarial): VERDICT PASS, 2
    MINOR, none blocking. Confirmed CLEAN: fix correctness (traced ixwebsocket
    threading + _extraHeaders re-read), thread-safety (header access is
    effectively single-threaded; watchdog in ws_shadow only counts silence,
    never reconnects), S4 (Error log prints only ixwebsocket's reason string,
    no key/sig material), fail-closed nullopt-signer path, red-first proof
    (empirically reproduced). MINOR-1 = max-backoff staleness (→ BACKLOG,
    d29c7e2). MINOR-2 = the "recovers/resubscribes" test assertion is not
    fix-dependent (the mock opens unconditionally) — the 3 timestamp
    assertions are the load-bearing proof; left as-is (still a valid
    resubscribe-path regression guard).
  - P4 capture continuity: the already-open data path (on_open/on_text/
    resubscribe/record/decode) is byte-for-byte unchanged; re-signing is added
    only on start/close/error, so the fix strictly CLOSES the capture gap and
    cannot introduce one.
- blocked / handoff: STEP 0 complete. Next fresh session starts **STEP 1 — AWS
  FULL MIGRATION** (docs/MASTER_SEQUENCE.md): first draft
  docs/PLAN_AWS_MIGRATION.md (seven-field Ws, §6 self-audit), then execute
  W-A0..W-A5 with the operator-approved riders. NOTE the STEP 0 fix is not yet
  deployed to the running Mac pipeline — it is committed on branch
  plan-live-validation-p0-p3 but the live ws_shadow is still the old binary;
  rebuild+redeploy of the pipeline binary (or the EC2 cutover in STEP 1) is
  what actually ends the 401-lockout exposure in production. Standing gates
  unchanged (fees OQ-1; 2026-07-13 seven-clean-days; live behind S1).

## 2026-07-07 11:53 UTC — MASTER SEQUENCE adopted as top-level queue (reorder, docs-only)

- commits: (this commit) docs/MASTER_SEQUENCE.md (new, operator sequence
  preserved VERBATIM) + CLAUDE.md queue pointer repointed to it + this entry.
  No code/pipeline/test changes; nothing touches a GUARDRAILS MUST.
- decisions:
  - The operator-authored FINAL MASTER SEQUENCE supersedes all prior
    orderings → docs/MASTER_SEQUENCE.md (verbatim). Prior queue
    (EXECUTION_PLAN + PLAN_GOLD_DATA_CONTRACT) is complete; the new queue is
    STEP 0 → STEP 6.
  - CLAUDE.md "Current execution queue" now points at MASTER_SEQUENCE.md, not
    EXECUTION_PLAN.md.
- context capsule: execution order for all future fresh sessions =
  docs/MASTER_SEQUENCE.md. STEP 0 is the earliest unfinished work and is
  URGENT: WS handshake auth headers are signed once at startup
  (ws_client.cpp:91); ixwebsocket auto-reconnect replays the stale signature
  ⇒ 401 loop until the hourly restart (root cause of the 2026-07-07 06:00–09:00
  UTC capture gap; clock exonerated — sntp 46ms, REST auth passing during the
  outage). Fix = re-sign fresh headers before EVERY connection attempt via the
  client-layer recovery ladder, red-first MockWebSocketTransport test, plus a
  quality_log entry, all in one commit. Standing gates unchanged: fees OQ-1
  awaits operator ratification; 2026-07-13 seven-clean-days gate; live trading
  behind ALL lifecycle gates + per-session operator confirmation (S1).
  Governing rule carried forward: one W per fresh session, independent audit
  after every W, exit ritual always.
- blocked / handoff: next session starts STEP 0 (WS reconnect signature fix).
  It has no plan doc of its own — it is a direct code fix (P4 note) per the
  MASTER_SEQUENCE STEP 0 spec. STEP 1 onward each requires drafting its named
  PLAN_*.md before executing its Ws.

## 2026-07-07 05:56 UTC — ENTIRE EXECUTION SEQUENCE COMPLETE (gold contract W1-W6 + EXECUTION_PLAN WP-00..09)

- commits: full chain fa18ff6(W1)..9343064(W6) — every WP/W landed with its own
  commit + independent-audit PASS in a separate fresh session; 4 audit-caught
  defects red-fixed (W1 FNV constant, WP-05 fees.verified->false, W6 F1/F1b
  work/raw guard), 3 real production incidents fixed with regression tests
  (taker_side BOOLEAN sniff, export/ingest race + shrink guard, ingest lock
  crash-loop)
- decisions:
  - Gold data contract PLAN_GOLD_DATA_CONTRACT W1-W6 fully built; day-06 gold
    certified GREEN (10.2M records, V2-V10 pass, V5/V7 verdicts filled)
  - fees.verified=false (fail-closed) blocks gate-mode net-profit until R
    ratifies OQ-1 -> config/kalshi_facts.yaml
  - depth expansion is DESIGN + operator-gated probe only (docs/PLAN_DEPTH_
    EXPANSION.md); probe NOT run (needs --operator-approved)
- context capsule: 107 tools registered, full pytest 281 passed + 1 xfailed
  (settlements-export xfail is an honest unbuilt-feature pin), make check ALL
  PASS, pipeline 4 procs alive (ws_shadow firehose + ingest daemon retry-
  hardened). Two NON-ENGINEERING gates remain before any trading: H-3 seven
  clean days (earliest 2026-07-13) and OQ-1 fee ratification (browser-confirm
  official PDF, flip fees.verified, rerun gate_calc). Gate-meeting input doc =
  work/mm/gate_report_<date>.md (day-06: 1809 signals, lifespan p50 1.0s, net
  REFUSED; unverified-preview median net -0.8c => naive bracket arb is
  net-negative after fees, confirming edge needs selection+pricing).
- blocked / handoff: sequence done. Next work is R-gated (fees ratify / clean-
  day clock) or a NEW plan (pricing model MM_ROADMAP 1.5, or gold W2.3 ws_seq
  adoption). Nothing auto-runnable is pending.
## 2026-07-07 — W6 DONE (probe-pending) — depth-expansion design doc + operator-gated probe

- commits: (this commit) docs/PLAN_DEPTH_EXPANSION.md + tools/depth_probe.py
  + tests/test_depth_probe.py (8 tests green) + tools.json appends
  (`depth_probe` kind=probe safety=network_read autorun=false needs
  creds+prod_env; `test_depth_probe`) + BACKLOG notes + this entry.
  NO production changes; apps/* untouched (read-only interface study only).
- decisions (each lives in the named file):
  - spec-derived limits table with evidence tags → PLAN_DEPTH_EXPANSION §1;
    the two binding limits (per-subscription market cap = asyncapi error 26,
    per-account connection cap) are NOT numerically documented — the probe
    empirically tests 50-markets-one-subscribe and 2-concurrent-connections
    as free side effects.
  - sizing = measured day-06 L1 rates per tier (read-only warehouse SQL,
    math shown) × full-depth multiplier calibrated on the 4-market watchlist
    sample (30–97×, n=3 in-game MLB — honest error bars); 50/200/500-market
    arithmetic in §3; expected 52/176/343 msg/s, conservative peak
    565/1,584/2,958 msg/s, 2.4–52.5 GB/day capture.
  - rollout options A (second depth instance) / B (extend firehose conn) /
    C (sharded multi-conn) enumerated with trade-offs, NONE chosen → §4,
    R decides in a separate operator-approved rollout plan.
  - probe protocol → §5: 900 s, top-50 of newest depth_target CSV, separate
    process + separate capture path work/probe/ (never work/raw, never
    work/metrics.ndjson — double-writer lesson), metrics redirected, zero
    REST tokens, refuses without --operator-approved (refusal demonstrated,
    exit 2), refuses live mode, refuses work/raw capture paths.
- blocked / handoff: **PROBE-PENDING** — measured msg-rate/bytes-by-tier
  table awaits the operator running `python3 tools/depth_probe.py
  --operator-approved` (15 min, watch freshness.py) and pasting the printed
  table into PLAN_DEPTH_EXPANSION §2/§3.

## 2026-07-07 05:15 UTC — WP-09 DONE (audit pending) — gate calculator: the three H-4 numbers by tested code

- commits: (this commit) tools/gate_calc.py + tests/test_gate_metrics.py
  (7 tests, TDD red→green: ModuleNotFoundError collection red pasted, then
  7 passed) + A1 wiring (tools.json `test_gate_metrics` pass_token "passed"
  + `gate_calc` kind=tool safety=pure; run_pipeline.sh suite line) + two
  BACKLOG notes + this entry. Report under work/mm/ (not committed, by
  the WP's allowed-writes rule).
- decisions (each lives in the named file):
  - signal = contiguous EPISODE of (100 − Σ L1 asks) ≥ min_edge_c over a
    fully-quoted bracket, never per-tick; default min_edge_c = 2.0¢ (sums
    ≤98¢ fire, the plan's 99–101¢ band silent), parameterized
    → tools/gate_calc.py docstring + pinned in tests/test_gate_metrics.py.
  - LOCF max quote age = 3600 s default (hourly-heartbeat cadence = the
    warehouse's documented change-only reconstruction bound; smaller drops
    valid state, larger trusts dead data), parameterized
    → tools/gate_calc.py docstring.
  - bracket universe: dims event_structure='bracket' preferred and CHECKED
    AT RUNTIME, but it is empty today (catalog lacks floor/cap_strike; dims
    today-only + 80k row cap) → documented fallback IS the effective path:
    event_ticker grouping + mutually_exclusive=true from events dim;
    ME-unknown events excluded fail-closed + counted; KXMVE prefix dropped
    (Q7) → tools/gate_calc.py + BACKLOG (catalog_sync rider).
  - full-quote guard: no evaluation until EVERY observed leg has a valid
    ask (0<ask_e4<10000) within the LOCF window — partial sums never fire
    (worst fake-signal class) → pinned by test_partial_quote_guard.
  - fees: mm_research.trade_fee is the ONLY fee fn (no second
    implementation); gate-mode net profit REFUSES on fees.verified=false
    (FeeNotVerifiedError, proven both directions on the real yaml + temp
    yamls); --preview-unverified-fees prints under a NON-GATE banner
    → tools/gate_calc.py + tests/test_gate_metrics.py.
- context capsule: REAL dry run 2026-07-07 ~05:05 UTC, defaults +
  --preview-unverified-fees: 2026-07-06 [FINAL] 8,210,082 L1 rows, 5,311
  events, 1,964 brackets evaluated (excl 1,304 single-leg / 874 ME-unknown /
  1,169 not-ME) → 1,809 signals; 2026-07-07 [PARTIAL] 6,903,405 rows, 2,209
  evaluated → 820 signals. Gate numbers: signal_count/day 1,314.5 (upper
  bound — exhaustiveness unverifiable, see BACKLOG), lifespan p50 1.0 s
  (p90 2,210.9 s, max 55,915.8 s, n=2,629, 257 censored at data end),
  net_profit REFUSED (OQ-1). NON-GATE preview @ unratified taker 0.07: net
  p50 −0.81¢/−0.77¢ per day — the p50 signal is net-NEGATIVE after taker
  fees (the hand-computed test fixture shows a 4¢ gross edge losing 0.57¢);
  positive tail exists (n(net>0) 648/291, likely non-exhaustive fragments).
  Report: work/mm/gate_report_2026-07-07.md (cites WP-06
  work/mm/research_2026-07-07.html, not recomputed). Implementation note:
  staging asks arrive as pandas nullable NA — bracket_signals coerces
  yes_ask_e4 to float64 (NaN) before the validity check (TypeError
  otherwise, hit on the real day-07 load). Suites: pytest 272 passed +
  1 xfail; make check tail ALL PASS; check_registry ok (105 tools).
  Pre-existing unstaged config/*.csv churn left alone per protocol.
- blocked / handoff: WP-09 needs its independent audit session (read-only,
  vs this diff). The gate meeting (H-4, human) is blocked on OQ-1 fee
  ratification (net_profit REFUSES until config/kalshi_facts.yaml
  fees.verified flips true — then rerun `python3 tools/gate_calc.py` for
  the real table; WP-07 fee-swap rerun also pending) and on the H-3
  clean-day clock (earliest admission 2026-07-13). All WP register items
  WP-00…WP-09 now built or closed; remaining: audits, WP-07, human steps.

## 2026-07-07 04:55 UTC — WP-08 DONE (audit pending) — daily quality check: the morning ritual as one command

- commits: (this commit) tools/daily_check.py + tests/test_daily_check.py
  (20 tests, TDD red→green: 20 collection/exec failures with the tool absent
  pasted, then 20 passed) + registry (tools.json `daily_check` kind=check
  safety=offline autorun=false pass_token "DAILY GREEN"; `test_daily_check`)
  + run_pipeline.sh suite line + BACKLOG + this entry.
- decisions (each lives in the named file):
  - freshness is CONSUMED via `python3 tools/freshness.py --json` subprocess,
    never reimplemented; test seam = `--freshness-json <file>` injecting the
    producer's exact JSON (rationale: test_freshness already proves the
    producer against a real Ingester-built staging; A1 one-infrastructure)
    → tools/daily_check.py docstring + tests/test_daily_check.py docstring.
  - hard failures (exit 1): freshness STALE/unreadable, manifest file missing,
    zero manifest rows for the completed day, md5 spot-check mismatch or
    manifest-listed file missing (write-once archive, D1/S2 fail-closed),
    gold day under <root>/quarantine/date=<D>[.N] OR validation verdict !=
    GREEN while the day sits in the green tree → tools/daily_check.py.
  - warnings (loud, exit stays 0): trades/orderbooks_l1 category row-count
    fall > --drop-pct (default 50%) vs prior-day manifest rows, gold built
    without a validation report, nonzero cumulative ws counters, unparseable
    quality_log lines → tools/daily_check.py.
  - quality_log finding extends the plan's <GREEN|WARNINGS:n> with RED:n —
    logging GREEN beside exit 1 would be the D2 green-lie; documented in the
    module docstring + pinned by test_quality_log_finding_red_on_hard_failure.
  - manifest history is deduped by (date, file_path), last row wins — a
    --force re-export's appended rows never double-count → read_manifest().
  - evidence records the measured staging lag every day (WP-03 BACKLOG note:
    a week of lag distribution before retuning the 600s threshold) →
    tools/daily_check.py + pinned in test_quality_log_append_is_schema_conformant.
- context capsule: REAL run 2026-07-07 ~04:49 UTC for window 2026-07-06:
  DAILY GREEN exit 0 — freshness FRESH (staging_lag 63.0s, capture 0.0s);
  manifest 223 rows (orderbooks_l1 8,210,082 / trades 1,913,798 /
  orderbooks_full 103,252), md5 spot 5/5; gold verdict GREEN (V2-V10 PASS),
  note: 2 row-level loader forensic files inside the day dir (known,
  quality_log'd W2.6); seq gaps: ws tail 400 lines, markers=0, all counters
  0, honestly "cumulative/not per-day"; 24h quality_log showed the 5 W2.6/
  WP-01 entries. Drop detection self-skipped (manifest holds ONLY 2026-07-06
  — first archived day); it arms itself after tonight's day-07 export. Two
  daily-check lines exist for the window (04:48 pre-lag-evidence, 04:49
  final) — append-only log, reruns are normal. Suites: pytest 265 passed +
  1 xfail; make check ALL PASS; check_registry ok (103 tools). Known
  pre-existing run_pipeline red (test_console lifecycle stage list, W4
  drift) is in BACKLOG, untouched (rule 1). Env override for the log path:
  DAILY_CHECK_QUALITY_LOG; tests never touch the real log.
- blocked / handoff: WP-08 needs its independent audit session (read-only,
  vs this diff). H-2 is live for R: run `python3 tools/daily_check.py` each
  morning and read it. Two BACKLOG riders added: per-day seq-gap
  instrumentation (ws_shadow untimestamped counters) and the gold
  not-built/unvalidated severity policy (R decision). Remaining WPs: WP-09
  (gate calculator; fees need WP-05 yaml — present) after WP-08's audit.

## 2026-07-07 04:37 UTC — WP-06 DONE (audit pending) — research notebook: K/z² + intervals, both spaces

- commits: (this commit) tools/mm_research.py + tests/test_research_metrics.py
  (9 tests, TDD red→green: ModuleNotFoundError collection red pasted, then
  green) + A1 wiring (tools.json test+tool entries, run_pipeline.sh line) +
  BACKLOG notes. Page/CSV under work/mm/ (gitignored by design, DoD says
  analytics/viz files only).
- decisions (files): **wiggle interpretation** — the plan fixture (mids
  [10,12,10,12] → K=12, z=2, wiggle=4) is degenerate between two readings;
  adopted K = Σ(Δmid)² (realized quadratic variation), z = net displacement,
  wiggle = (K−z²)/2 (mean-reversion-harvest bound), reversal_rate =
  sign-flips/(n_moves−1), space-invariant. Rationale + rejected reading in
  tools/mm_research.py docstring; R-confirm note in docs/BACKLOG.md.
  Heartbeat = is_snapshot AND price_e4 NULL (first-obs snapshots kept);
  intervals nearest-rank (gold convention); book-validity filter identical
  to mm_calibrate (bid>0, ask<100c, ask>bid). Fee guard READS
  config/kalshi_facts.yaml at call time; gate_mode with verified!=true
  raises FeeNotVerifiedError (currently false ⇒ refuses — live-tested in
  the suite against the real yaml AND mutated temp yamls).
- real run (2026-07-06 archived + 2026-07-07 staging PARTIAL): 14,649,242
  L1 rows → 675,800 heartbeats excluded → 11,423,208 valid → 40,550
  market-days. A2 divergence is real and large: price-space top-10 is
  Crypto-heavy (KXSOLE 26JUL0717 B78-B84 bracket, KXSHIBAD); log-odds
  top-10 promotes Commodities strikes (KXNATGASMON-26JUL3117-T2.499 px#6→
  lo#1, KXCOPPERW-26JUL1017 family px#21..47→lo#2..9) and demotes mid-range
  SOL brackets (B82 px#9→lo#47, Δ−38; SHIBAD px#2→lo#19). Exactly the
  vol∝p(1−p) overweighting A2 predicted. Page:
  work/mm/research_2026-07-07.html (68 KB, tables + inline SVG, stdlib-only,
  light/dark) + research_metrics_2026-07-07.csv (all 40,550 rows).
- battery: pytest 245 passed + 1 xfailed; make check tail ALL PASS;
  run_pipeline.sh PIPELINE PASS; check_registry ok (101 tools).
- pipeline continuity (P4): read-only throughout — load() only, ATTACH
  READ_ONLY with lock retry; no capture/ingest/export file touched.
- blocked / handoff: WP-06 independent audit pending per protocol. WP-07
  (fee swap re-run) stays BLOCKED on R ratifying fees provenance (OQ-1);
  when kalshi_facts.yaml flips verified:true, test_fee_placeholder_guard's
  reality assertion will flag for the WP-07 revisit by design. Rankings are
  gross wiggle — no fees/fills/simulation anywhere (WP-06 scope).

## 2026-07-07 — W5 DONE — ws_sid/ws_seq into orderbooks_full (the one production-adjacent change)

- commits: (this commit) tools/ingest.py + tests/test_ingest.py +
  tests/test_export_day.py + docs/warehouse_schema.md (E5 same commit).
- what landed: two nullable BIGINT columns `ws_sid`/`ws_seq` on
  orderbooks_full, populated from frame-level `sid`/`seq` (top-level in the
  WS frame, NOT in msg — W3.3 verified) for orderbook_snapshot /
  orderbook_delta; anything missing/non-int ⇒ NULL (`ws_int()`, D3).
  Migration: `Ingester._migrate_full_seq()` PRAGMA-checks and ALTERs the
  columns into a pre-W5 13-col staging table on init (nullable, instant,
  idempotent). INSERT switched to an explicit column list (`FULL_INSERT`)
  so it is correct on both fresh and migrated DBs.
- export_day.py: UNCHANGED — verified its `SELECT *` picks the columns up
  (test "archived parquet carries ws_sid/ws_seq"); load()'s
  union_by_name/UNION ALL BY NAME proven to union a 13-col pre-W5 archive
  with a new archive, missing cols ⇒ NULL (test 5b in test_export_day.py).
- TDD: both suites red first (BinderException: "ws_sid" not found in
  SELECT — exact missing feature) then green; full battery green:
  pytest 236 passed + 1 xfailed, make check ALL PASS, run_pipeline.sh
  PIPELINE PASS, check_registry ok (99 tools).
- continuity (P4): capture side untouched (ws_shadow already writes raw
  frames containing sid/seq); the ONLY production restart was the ingest
  daemon (checkpoint byte offsets make restart gap/dup-free). Restarted
  pid 35544, fresh [ingest] +L1/+trades lines within 90s; live staging
  migrated to 15 columns; all 103,252 pre-W5 rows read NULL ws_seq —
  expected, and stays NULL until a watchlist capture produces orderbook
  frames (firehose subscribes ticker+trade only).
- rollback: revert commit + restart ingester (checkpoint-safe). The
  ALTER-added columns are harmless if code reverts (nullable, ignored by
  the old 13-value positional INSERT? NO — old positional INSERT would
  break on 15 cols; a revert must also either drop the columns or rely on
  the reverted DDL creating a fresh staging — noted: revert commit ⇒ the
  old INSERT is positional 13-of-15 and DuckDB rejects it, so on rollback
  ALSO run: ALTER TABLE orderbooks_full DROP COLUMN ws_sid; DROP COLUMN
  ws_seq; (or delete staging.duckdb — rebuildable from raw, D1).
- gold adoption of ws_seq (W2.3 merge preferring real seq, per-sid gap
  detection post-W5 mode) is OUT of W5 scope — future workstream; gold
  builds on pre-W5 data keep recording `seq_unavailable`.
- blocked / handoff: PLAN_GOLD_DATA_CONTRACT W1→W5 all landed; W6
  (depth-expansion design + probe) is operator-gated. Independent audit of
  W5 pending per protocol.

## 2026-07-07 — WP-04 DONE (audit pending) + WP-01 CLOSED — permanent acceptance suite

- commits: (this commit) tests/test_pipeline_contract.py (12 tests, TDD
  red→green: exit-4 collection red, then 5 genuine assert fails during
  development, then green) + A1 wiring (tools.json entry pass_token
  "passed", run_pipeline.sh line) + BACKLOG notes.
- WP-04: new contracts — test_every_byte_accounted (checkpoint offsets =
  100% of complete-line bytes incl. rotation shard; partial tail excluded
  until completed, then counted exactly once), test_league_parse
  (KXMLB→MLB, KXKBO→KBO, KXNCAABB→NCAABB known_league; longest-match
  NCAAFB≠NCAAF; unknown sports prefix → family_prefix + needs_review=True —
  the plan's "_unknown + warning" maps to the review flag + V16 Class-B
  default, documented in-test), test_settlement_partitioned_by_settled_date
  (xfail strict=False: exporter has NO settlements fact — feature not built
  per protocol rule 1, BACKLOG filed), test_denormalized_columns_present
  (every archived l1+trade row carries category/subcategory/group/series/
  event, both classes), test_load_routing (late-staged row after export
  does NOT change the archived day = past reads archive; today reads live
  staging; no overlap double-count). Adopted per A1 by importing
  test_ingest helpers with independent fixture values: test_change_only
  (57+1+qty-only-change → 3 rows, full state tuple), test_heartbeat (3
  quiet hours → 3 hour-start heartbeats, state carried, NULL price),
  test_kill_restart_determinism (mid-line byte cut + fresh Ingester →
  row-for-row EXCEPT ALL equality both directions, all 3 tables).
- WP-01 CLOSED: folded tests green (test_e4_roundtrip "0.0325"→325→
  presented 0.0325 exactly via load(); "0.5000"→5000; legacy integer-cent
  levels_e4 *100; test_fractional_qty "5119.00"→51190000, "0.7500"→7500);
  quality_log entry appended (work/quality_log.ndjson, wp=WP-01, window
  2026-07-06T08:18-08:35Z, discarded-unrecoverable); live load() sample:
  2026-07-06 trades 1,913,798 total / 398,045 sub-penny (20.8%), e.g.
  EUCLIMATE-2030 e4=4140 → $0.4140.
- suite state: full pytest 236 passed + 1 xfailed; make check green;
  check_registry ok (99 tools). run_pipeline: all suites pass EXCEPT
  test_console — PRE-EXISTING W4 stage-list drift (verified on clean HEAD
  via stash), BACKLOG'd, out of WP-04 scope.
- blocked / handoff: independent audit session must run before WP-04 is
  marked DONE (anti-tautology check incl. adopted tests); test_console
  drift fix is a one-line expected-list update for a W4 rider; settlements
  export remains BACKLOG (R decision).

## 2026-07-07 — W4 DONE — coverage auditor (S1-S4, V14/V15/V16) + lifecycle research stage

- commits: (this commit) tools/coverage_audit.py + tests/test_coverage_audit.py
  (27 tests, TDD red→green) + lifecycle_check.py appended non-blocking
  "Coverage Audit (research)" stage + tools.json/run_pipeline.sh entries +
  docs/next_actions.md + BACKLOG notes.
- decisions (all in files):
  - V15 declared-list semantics → `tools/coverage_audit.py`: the firehose
    subscribes no orderbook_delta and NO declared watchlist file exists, so
    V15 reports `declared_list_missing` (documented, never invented); the 4
    observed full-depth markets on 2026-07-06 are legacy watchlist leftovers
    (KXMLBSPREAD/TOTAL-26JUL052130BOSLAA-*, KXWCGOAL-26JUL05MEXENG-…).
    Shrinkage vs a declared list = ERROR exit 1 (proven live + fixture).
  - Sanitized-category classification → `classify_category`: warehouse fact
    rows store wc.sanitize()d categories; classifier matches both spellings;
    `_unclassified` = unknown-category leakage, not a "new category" (V16).
  - Supervisor wiring + config/depth_watchlist.txt creation are
    OPERATOR-GATED → `docs/next_actions.md` (W4 forbidden-writes, P4).
- real audit 2026-07-06: traded=127,997 / L1=44,569 / full-depth=4;
  V14: 22,477 of 25,600 High+Mid lack L1 — ZERO Class A violations (all
  Class B by policy: 22,110 Exotics/MVE Q7-excluded + 367 promotion
  candidates → work/mm/promotion_candidates_2026-07-06.csv); S3: 5,274
  Sports traded, 2 missed (KXITFWMATCH-262799.99, KXITFMATCH-26JUL06BEASCO-
  SCR-26-EHAA, both Low tier); S4 → work/mm/depth_target_2026-07-06.csv
  (9,469 scoreable); V16 warns only on 43 null-category markets + 2
  catalog-missing series (KXMLBWINS, KXNEWOUTBREAK).
- blocked / handoff: promotion CSV is report-only — operator decides
  per-market vs wholesale promotion after a week of reports (plan §7);
  depth_target CSV feeds W6 (design+probe, operator-gated).

## 2026-07-07 04:05 UTC — WP-05 DONE — Discovery Mission: API ground truth report + kalshi_facts.yaml

- commits: (this commit) WP-05 discovery report + config/kalshi_facts.yaml +
  sandbox/discovery probe scripts/outputs + BACKLOG additions.
- decisions (all live in files, none chat-only):
  - Kalshi ground-truth constants → `config/kalshi_facts.yaml` (fees
    verified=true from official schedule w/ provenance caveat, rate limits
    VERIFIED-LIVE, endpoint existence, demo status, RTT/lag numbers).
    Downstream code imports this; no hardcoded fees/limits ever (WP-05
    contract). WP-07/WP-09 fee dependency is now UNBLOCKED pending R's
    ratification of OPEN QUESTION 1.
  - Full findings + 6 OPEN QUESTIONS + 5 CORRECTIONS →
    `docs/DISCOVERY_REPORT_2026-07-07.md`. Out-of-scope code fixes → BACKLOG
    (env.cpp stale demo message, request_spec F8 comment, sub-penny builder,
    clock-skew telemetry). No production code/data touched (read-only
    mission; A2 env gates untouched).
- context capsule (for a fresh session):
  - Headline live findings: (1) DEMO ENV IS UP (both hosts HTTP 200,
    2 shards) — engine's "unavailable" premise stale, policy still correct;
    (2) legacy POST /portfolio/orders REMOVED from spec — our wire.hpp
    already targets V2 /portfolio/events/orders and field names match spec
    exactly; (3) tier advanced = 300/s refill AND 600 capacity (2s burst)
    both buckets; cancel costs 2 tokens, create 10; no quota headers exist
    (200 or 429); GET /markets is CDN-cached ~15s (Q5 reinforcement);
    (4) private WS subscribe VERIFIED-LIVE: fill/user_orders/
    market_positions ack per-channel with sids, server pings 10s "heartbeat";
    (5) official fees: taker ceil(0.07·C·P·(1−P)), maker ceil(0.0175·…)
    ONLY on fee_type=quadratic_with_maker_fees series — KXNBA has maker fees
    LIVE, KXBTCD is plain quadratic; rounding is now centicent-ceil +
    per-order accumulator (fee_rounding.md), NOT cent-ceil (old PDF);
    (6) measured: RTT warm p50 35.6ms n=20; decode+apply 281.9 ns/msg;
    feed inter-arrival p50 10.2µs / p99 63ms on clean hour-12 capture
    (hour-08 splice window yields garbage extremes — filter epochs).
  - Gap-register spot checks all CONFIRMED (items 1,3,6,8,13); review deltas:
    gold layer + day-06 archive exist now.
  - Dead ends ruled out: kalshi.com fee PDF is Vercel-bot-gated (curl+
    WebFetch both fail) — used archive.org snapshot 2026-02-18 sha256
    b1a37aa7…; python has no cryptography/websockets libs — WS probe is
    stdlib + vendored OpenSSL 3.5.7 CLI signing (sandbox/discovery/
    ws_private_probe.py, reusable pattern for future probes).
  - Probe budget spent: ~240 read tokens, 0 write, 1 WS connection.
- blocked / handoff: R must adjudicate the 6 OPEN QUESTIONS (fee provenance,
  demo strategy, sub-penny builder, order-group dead-man, skew telemetry,
  batch-read billing probe). Sandbox scripts stay until report ACCEPTED (A6),
  then delete. WP-07 can start once OQ-1 is ratified.

## 2026-07-07 03:10 UTC — WP-03 DONE — freshness monitor: staging + capture lag, one command, STALE alarm

- commits: this commit (tools/freshness.py + tests/test_freshness.py +
  A1 registry appends: tools.json `freshness` check (pass_token FRESH) +
  `test_freshness` test entry + run_pipeline.sh suite line + BACKLOG
  notes). Nothing else touched; config/*.csv churn left unstaged.
- TDD: RED proven (8 failed, implementation absent — subprocess "No such
  file tools/freshness.py"), then 8 passed; full ./tests/run_pytest.sh
  198 passed; check_registry ok (96 tools); make check tail ALL PASS.
- decisions (rationale in tools/freshness.py docstring, E2):
  - TWO lags, alert if EITHER > threshold (default 600s, --threshold):
    (a) staging lag = now − max(ts_utc) over ALL fact tables
    (orderbooks_l1, trades, orderbooks_full — the test fixture puts the
    newest row in `trades` so an L1-only tool is rejected); (b) capture
    lag = now − newest *.ndjson* mtime under work/raw/date=<today>/
    (yesterday's dir also scanned so the first seconds after UTC midnight
    don't false-alarm; glob matches rotation shards .ndjson.N — the exact
    file class the shard incident missed, and there is a test for it).
    This split distinguishes "capture died" from "ingest behind" — both
    2026-07 incidents were capture-fine/staging-stale.
  - fail-closed (S2): missing staging, empty fact tables, no raw files,
    or read-only connect still locked after the retry window (12×5s,
    reader-side mirror of ingest.py connect_with_retry) ⇒ STALE exit 1
    with an explicit "unmeasurable" reason — never green on a metric we
    could not read. Verdict word FRESH appears only on pass (registry
    pass_token; exit code authoritative). --json for WP-08; --now/
    --staging/--raw-root injection for deterministic tests.
- live demo (DoD): injected-stale tmp fixture ⇒ VERDICT STALE exit 1
  (staging lag 93398.1s, capture lag 7207.4s, both reasons printed);
  REAL pipeline ⇒ VERDICT FRESH exit 0, staging lag 48.3s (newest
  ts_utc 2026-07-07T03:05:07.418Z via trades), capture lag 0.13s
  (work/raw/date=2026-07-07/firehose_03.ndjson) — read live against the
  running ingest daemon without disturbing it.
- blocked / handoff: WP-08 consumes `python3 tools/freshness.py --json`
  (fields: verdict, staging_lag_s, capture_lag_s, stale_reasons, ...);
  two BACKLOG notes for WP-08 (quiet-period threshold observation;
  branch on stale_reasons for behind-vs-unreadable paging).

## 2026-07-07 02:57 UTC — W3.2 DONE — V7 race/consistency report (REPORT-ONLY) measured on the real day, manifest verdict filled

- commits: this commit (tools/gold_v7_race.py + tests/test_gold_v7_race.py
  + committed v7_inverted_taker seeded-defect fixture + A1 registry appends
  (tools.json test+check entries, run_pipeline.sh line) + BACKLOG notes;
  nothing else touched — work/gold data and config/*.csv churn not
  committed). NOTE: implementation/tests/fixture/registry were written by
  the prior (interrupted) W3.2 session and left uncommitted; this session
  verified everything red/green from scratch, re-ran the real day live,
  and performed the exit ritual.
- TDD: RED proven (ModuleNotFoundError collection error with the
  implementation absent), then 16 passed; full ./tests/run_pytest.sh 190
  passed; check_registry ok (94 tools); make check tail ALL PASS.
- decisions (rationale in tools/gold_v7_race.py docstring, E2):
  - measurement: per TRADE record, print at the as-of touch? taker=yes ⇒
    trade_yes_price_e4 == ask_px_e4[0], taker=no ⇒ == bid_px_e4[0]; the
    as-of book is the state ON the trade record (§2.2 item 4 guarantees
    pre-trade). Vectorized numpy bincounts over 1.9M trades (~60 s day).
  - two populations NEVER pooled: covered_book (F_BOOK_COVERED, real
    full-depth as-of) vs l1_asof (uncovered; L1 state in slot 0).
  - honesty split (D2): invalid as-of book (incl. book_seq 0) /
    empty reference side / unknown taker_side = UNMEASURABLE buckets
    (book_invalid / side_empty / bad_taker_side), never races.
  - REPORT-ONLY binding (§2.2 point 5): NO threshold-enforcement path —
    module can only `return 0`; grep-proven by test (no `return [1-9]`,
    no sys.exit except sys.exit(main())). Proposals = nearest-rank p95 of
    per-market rates per population per category (+_global), pool =
    markets with ≥ 20 measurable trades (MIN_MEASURABLE, mirrors V5
    min-support). Slices above proposal ⇒ unsafe_for_microstructure=true
    in report + manifest — marked, never failed.
  - market class A/B from config/market_classes.yaml categories with
    PATH-SANITIZATION normalization ("Climate_and_Weather" sidecar vs
    "Climate and Weather" yaml — test-pinned); unknown category ⇒ B (V16).
  - subcategory not in the markets sidecar (BACKLOG W3.1 note) — fetched
    read-only from warehouse trades rows with a lock-retry loop; source
    recorded in the report (subcategory_source).
  - manifest: update_manifest_v7 per the V5 pattern — certified md5s AND
    V5 verdict asserted byte-identical, atomic replace, reader re-opened
    (W2.4 BACKLOG note now FULLY resolved).
- REAL day 2026-07-06 result (exit 0; work/gold/date=2026-07-06/
  v7_race_report_2026-07-06.json, 62 MB; verdict in manifest):
  support n_records=10,222,410, n_trade_records=1,909,088,
  n_markets_traded=127,994. covered_book: 3 markets / 744 trades / 134
  races = 0.180 (all Sports/Baseball/High/A). l1_asof: 1,540,431
  measurable / 394,096 races = 0.2558; unmeasurable 367,913 (book_invalid
  367,557 — dominated by Exotics/class-B traded-no-L1, measurable 0 by
  V10-honest construction; side_empty 356). By category (l1_asof):
  Sports 0.1435, Crypto 0.4051, Climate 0.3362, Financials 0.1545;
  Soccer subcat 0.031 vs BTC subcat 0.442 (15-min crypto ladders are the
  race hotspot). Proposed thresholds (FOR OPERATOR APPROVAL — not
  enforced): l1_asof _global 0.5714 (pool 3,012 mkts), Sports 0.5,
  Crypto 0.6207, Financials 0.4, Climate 0.5607, Commodities/Politics
  0.6667, Economics 0.7826; covered_book _global 0.2046 (pool 3).
  147 slices marked Unsafe for Microstructure Backtest (143 markets +
  3 subcats GDP/HYPE/Local + 1 more) — nothing failed, exit 0.
  Interpretation caveat (BACKLOG): ms-granular capture ts ⇒ l1_asof rates
  upper-bound true races (channel-alignment noise included); day-one
  baseline sample, not global truth (G4).
- red-proof (anti-fake-green): committed fixture tests/fixtures/
  gold_defects/v7_inverted_taker/ = 25 prints exactly at the correct
  touch with every taker_side FLIPPED — correct-sided twin measures
  race_rate 0.0, the fixture measures 1.0 (measurement catches the
  inversion); CLI on the defect day still exits 0 (NOT a threshold
  failure — report-only proven on the defect itself).
- blocked / handoff: next is W4 (coverage auditor) per the workstream
  order; W3.2 day-one thresholds await operator approval before any
  category threshold may become blocking (a separate, operator-gated
  change — no enforcement code exists yet by design).

## 2026-07-07 02:30 UTC — W3.1 DONE — δ distribution (V5) measured on the real day, manifest verdict filled

- commits: this commit (tools/gold_v5_delta.py + tests/test_gold_v5_delta.py
  + committed v5_shifted_book seeded-defect fixture + A1 registry appends +
  BACKLOG notes; nothing else touched — work/gold data not committed).
- TDD: suite written first, RED proven (ModuleNotFoundError collection
  error), then implementation → 14 passed; full ./tests/run_pytest.sh 174
  passed; check_registry ok (92 tools); make check tail ALL PASS.
- decisions (rationale in tools/gold_v5_delta.py docstring, E2):
  - δ per L1 change row = |Δts| to the NEAREST valid covered book record
    (BOOK_SNAPSHOT/BOOK_DELTA, F_BOOK_VALID) whose top (bid_px_e4[0]/
    ask_px_e4[0], empty sides normalized to L1's 0/10000 sentinels) equals
    the L1 view, inside a FIXED ±5 s scan window. SCAN_BOUND_US is a code
    constant, deliberately NOT a CLI flag (§2.3 V5: widening δ to absorb
    mismatches is FORBIDDEN — test-asserted that no bound/window CLI knob
    exists).
  - honesty split (D2): "never_agree" (book records in-window, none agrees)
    is a real mismatch; "uncheckable" (no book record in-window at all) is
    reported separately and NEVER counted as a mismatch — covered capture
    ran 0.5 h, L1 rows ran all day.
  - bad-markets rule: mismatch_rate_at_global_p99 > 0.05 (5x the ~1%
    beyond-p99 by construction) AND n_checkable >= 20 (below that a market
    cannot be condemned; its mismatches still count). Percentiles are
    nearest-rank on µs ints; ms only at the render edge.
  - L1 views come from the warehouse at report time (gold .bin cannot carry
    them: for covered markets the FSM state at an L1_TICKER record is the
    full-depth book; payloads are not serialized) — routed through the
    W2.1 load_l1 gates; scheduler heartbeats excluded and counted (72).
  - manifest verdict update: safety_verdicts.V5 replaced in place, certified
    md5s asserted byte-identical, atomic tmp+os.replace, GoldDayReader
    re-opened to prove certification (BACKLOG W2.4 note partially resolved;
    V7 half stays for W3.2). Report file NOT added to manifest["files"].
- REAL day 2026-07-06 result (exit 0, work/gold/date=2026-07-06/
  v5_delta_report_2026-07-06.json; verdict in the manifest):
  *** BASELINE SAMPLE (support: 4 markets) *** — NOT global truth (G4).
  support: n_markets=4, capture_hours=0.5, n_l1_rows=1767,
  n_full_depth_rows=103252, n_matched_pairs=1731.
  global delta_ms p50=0.000 p90=0.000 p99=0.000 max=245.411;
  mismatches: never_agree=0, beyond_global_p99=1, uncheckable=36;
  bad markets: none (the WCGOAL market has rate@p99=1.0 but only 1
  checkable row — min-support rule correctly refuses to flag on n=1).
  Per market: KXMLBTOTAL…-14 709/709 matched δ=0; KXMLBSPREAD…-BOS5
  628/628 δ=0; KXMLBTOTAL…-16 393/393 δ=0; KXWCGOAL… 1 matched δ=245.4 ms,
  20 uncheckable (its book has only 2 snapshots, L1 spread over the day).
  Cross-check: warehouse L1 fetch for the 4 tickers = 1767 rows, exactly
  the gold day's covered L1_TICKER count (0 rejected).
  Why δ≈0: capture timestamps are ms-granular (L1 ts 100% and book ts
  99.995% end in 000 µs) and ticker+delta frames for the same book event
  land in the same capture ms — day-one δ measures same-clock capture
  alignment, not cross-channel latency; do not read it as physics.
- red-proof (anti-fake-green): committed fixture tests/fixtures/
  gold_defects/v5_shifted_book/ = book tops price-shifted +100 E4 vs its
  committed l1_views CSV ⇒ 25/25 checkable rows never_agree, market
  flagged on bad_markets, δ pool EMPTY (delta_ms=None — no fake δ), and a
  100x scan bound STILL cannot absorb it (test-asserted); CLI on it exits
  1 with "BAD MARKETS SURFACED".
- blocked / handoff: W3.2 (V7 race/consistency report) is next in the gold
  plan; it needs the same warehouse subcategory lookup (sidecar has no
  subcategory column — BACKLOG note filed) and the same manifest-verdict
  helper pattern. warehouse.load() staging-ATTACH retry (12 s) was
  exhausted once during the real run (ingest lock burst) — outer retry
  succeeded; BACKLOG note filed.

## 2026-07-07 01:46 UTC — W3.3 DONE — golden Kalshi frames (V13) pinned on real captures

- commits: this commit (tests/test_kalshi_golden.py + 101 real verbatim
  frames under tests/fixtures/kalshi_golden/ + 3 doctored red-proof
  fixtures under tests/fixtures/gold_defects/golden_* + A1 registry
  appends; nothing else touched).
- fixtures (real, verbatim RawRecord lines; §2.2-style gates applied at
  sampling, 0 gate-skips in the sampled regions — provenance table in
  tests/fixtures/kalshi_golden/README.md):
  - 1 orderbook_snapshot + 50 orderbook_delta from work/live_capture.ndjson
    (2026-07-06 watchlist capture, 03:14–03:44 UTC) — fallback per the WP:
    the 24/7 firehose subscribes ticker+trade only (verified: 0 orderbook
    frames in a 200k-line sample).
  - 50 trades from work/raw/date=2026-07-06/firehose_12.ndjson
    (12:00–12:01 UTC, 36 markets), outside the 08:18–08:35 splice window
    by construction of the source file, with validation as the actual gate.
- REAL semantics discovered and pinned (tests + README):
  - trade frames DO carry sid+seq (doc I9 drift → BACKLOG);
  - snapshots share the sid seq counter; in-stream get_snapshot stamps the
    NEXT sid seq (protocol doc open question #2 answered, seq 2025 observed);
  - delta `ts` = ISO-8601 Zulu string with variable-length fraction
    (".52251Z" breaks fromisoformat) vs trade `ts` = epoch-seconds int;
    snapshot msg has NO ts fields (→ BACKLOG parser-audit note);
  - all prices/qty string fixed-point, byte-exact E4 round-trip via
    gold_load parse_e4/render_e4 on all 101 frames; yes+no price == $1
    on every trade; taker_side strictly yes/no (taker_outcome_side/
    taker_book_side also present).
- V12 wiring: module-level pytest.skip (loud operator message) when saved
  work/kalshi_spec_alignment.json is missing/red/>7d — logic mirrors
  gold_build.spec_gate (not imported: avoids the duckdb/warehouse stack);
  gate proven able to go red on synthetic missing/red/stale files.
- red/green: suite first run 15 failed (fixtures absent, TDD) → extraction
  → 16 passed; doctored dup-trade_id / seq-regression / float-price
  fixtures caught via the SAME checkers the real-frame tests use.
- acceptance demonstrated: ./tests/run_pytest.sh full = 160 passed
  (fixture git-tracking assertion included); tools/check_registry.py ok
  (90 tools). make check NOT run (background gold rebuild running, per WP).
- next: independent audit session for W3.3; protocol-doc drift rider
  (BACKLOG) needs an owner.

## 2026-07-07 01:30 UTC — W2.6 DONE (first real build GREEN) + archive taker_side narrowing found (day is trade-less, rebuild needed)

- commits: this commit (W2.6: tools/gold_build.py thin composition +
  registry entry + 6 BACKLOG notes; no module modified, no tests file —
  composition only per plan, smoke-verified via the real build)
- decisions (rationale in tools/gold_build.py docstring, E2):
  - HEARTBEAT mapping (closes W2.2/W2.3 note): L1 rows with
    is_snapshot=true AND NULL price_e4/volume_e4/open_interest_e4 = the
    ingester's hourly scheduler heartbeats → routed through the FULL W2.1
    L1 gates as source "orderbooks_l1_heartbeat", then re-kinded via
    Event._replace(kind=HEARTBEAT); is_snapshot=true WITH price data
    stays L1_TICKER. Real day: 428,204 of 8,108,826 L1 rows.
  - Merge-order disposition (closes W2.3/W2.5 policy question): merge
    sources are per-(channel, market) slices, each STABLE-sorted by ts_us
    at compose time — "Merge Order Violation" is structurally unreachable
    from loader output; same-ts intra-slice order preserves load() row
    order (Timsort); loader ts_regressions stay report-only (0 on the
    real day).
  - Liquidity tier (sidecar metadata): traded markets ranked by summed
    count_e4 desc (ties by ticker): High = top decile, Mid = next decile,
    Low = rest incl. untraded. (This day: all Low — zero accepted trades,
    see below.)
  - V12 gate reads SAVED work/kalshi_spec_alignment.json (status=pass AND
    ≤7 days old; was pass/0.73d); stale/red ⇒ exit 2 + operator
    instruction; the build NEVER auto-runs the network sync.
  - loader outputs written INSIDE the day partition so a validator
    quarantine moves the forensics with the day.
- acceptance demonstrated (real day 2026-07-06, exit 0, DAY GREEN):
  8,212,066 records / 44,442 markets / 4 covered; .bin 4,204,577,792 B
  (= 8,212,066×512); FSM clean (0 Invalid Book State / Sequence Gap /
  negative-delta; 428,204 heartbeats neutral; 1 crossed book flagged);
  validator matrix V2/V3/V4/V6/V8/V9/V10 all PASS; runtime 539.7 s,
  ru_maxrss 6.33 GB (peak footprint 17.7 GB incl. compressor);
  make check ALL PASS; pytest 144 passed; check_registry ok (89).
- context capsule — CRITICAL FINDINGS (full details + evidence in
  docs/BACKLOG.md 2026-07-07 entries):
  1. taker_side SILENT NARROWING: warehouse.py load("trades") on ARCHIVED
     days lets DuckDB read_csv sniff the yes/no column as BOOLEAN → comes
     back 'true'/'false'. Archive csv.gz verified to hold 'yes'/'no' raw.
     Consequence: the W2.1 gate quarantined ALL 1,863,197 archived trades
     (fail-closed, correct, nothing repaired) → gold date=2026-07-06 has
     ZERO TRADE records and all-Low tiers. The manifest's source_day
     carries the loader summaries, so the partition self-describes this.
     REBUILD the day (derived, deletable) after fixing load() typing
     (explicit types on read_csv) — warehouse.py is forbidden-writes for
     gold WPs, so the fix is an operator/rider change WITH regression
     test. Staging (VARCHAR) was clean — that is why W2.1's demo passed.
  2. trades EXPORT SHORTFALL: archive holds 1,863,231 day-06 rows but
     staging at 01:00 UTC still held 1,909,095 DISTINCT day-06 trade_ids
     → ~45.9k unique trades missing from the write-once archive (ingest
     lag vs midnight export cut?); invisible via load(); lost at staging
     prune unless reconciled.
  3. close_time resolves for only 72/44,442 tickers (catalog dim = 80,000
     open markets, settled intraday markets absent) — sidecar close_time
     left empty, surfaced; Q6 work needs a catalog retention story.
  - perf facts for W-BENCH: fetch 13 s; loaders 76 s; merge 137 s
    (81,515 sources); write_day 53 s; validate_day 251 s. Real day is
    ~12.6× the plan's ~650k/day estimate (8.2M records, 4.2 GB/day
    uncompressed → G8 window ≈ 59 GB, fine on this disk).
- blocked / handoff: gold date=2026-07-06 partition is GREEN but
  trade-less — do not use for trade research; rebuild after the
  warehouse.py taker_side fix (BACKLOG owns it). W3.1 (δ distribution)
  is next per the plan and is meaningful on book data now; W3.2 (V7
  race report) needs the rebuilt day with trades.

- commits: this commit (W2.5: tools/gold_validate.py + tests/
  test_gold_validate.py + 6 seeded-defect gold day fixtures + registry
  wiring + 3 BACKLOG notes)
- decisions (rationale in tools/gold_validate.py docstring, E2):
  - checks: V2, V3, V4, V6, V9, V10 run INDEPENDENTLY over a written day,
    PLUS the V8-shape gold_io.reconcile_trade_hashes wired into every run
    (W2.4 finding: must not stay dormant). A crash inside one check is
    caught as that check's failure — never masks the others.
  - all record access via RawDay, an md5-BLIND reader, so a V2 md5/
    manifest failure cannot stop V3-V10; V2 verifies explicitly (every
    manifest md5, record/trade/market counts vs parsed rows) AND surfaces
    the strict GoldDayReader's open-time refusals as reported violations
  - V3/V6 REUSE gold_merge.v3_violations/v6_violations verbatim on shims
    from .bin rows ("minted" inferred as book_seq exceeding the market's
    previous value — sound, not circular; V9 owns heartbeats explicitly)
  - quarantine = MOVE the whole partition to work/gold/quarantine/
    date=<D>[.N] (collision suffixed, nothing deleted/overwritten, P6) +
    validation_report_<D>.json written inside; CLI exits nonzero. Moving
    beats a marker file: the green tree cannot resolve the day by path
    (S2), partition stays byte-intact for forensics
  - six committed defect fixture days (v2_count_mismatch, v3_stream_seq_
    gap, v4_negative_level, v6_trade_lookahead, v9_heartbeat_diff,
    v10_coverage_lie), built via the W2.4 writer + targeted tampering
    with manifest md5s made self-consistent (so the CHECK fails, not the
    md5 gate; v2's manifest defect IS its check); deterministic
    regeneration: python3 tests/test_gold_validate.py
- acceptance demonstrated: suite RED first (ModuleNotFoundError:
  tools.gold_validate), then 12 tests green; per-check matrix printed —
  good day all-PASS GREEN; each defect day FAILs exactly its own check
  (all six others PASS) => QUARANTINE; V8 uuid-tamper day red with V2
  green (md5 gate not the catch); CLI demo: good day exit 0 in place,
  defect day exit 1 + partition moved under quarantine/ with report;
  full pytest 144 passed; make check ALL PASS; run_pipeline.sh
  == PIPELINE PASS == (test_gold_validate wired); check_registry ok (88)
- rollback: revert commit + delete work/gold/ (derived, rebuildable)
- next: W2.6 first real build (gold_build.py = thin composition of
  W2.1-W2.5, no new logic, --date 2026-07-06)

## 2026-07-06 — W2.4 DONE (gold writer/reader, TDD red-first, mutation/tamper-proven)

- commits: 7aebacb (W2.4: tools/gold_io.py + tests/test_gold_io.py + 2 io
  defect fixtures + registry wiring + 4 BACKLOG notes)
- decisions (rationale in tools/gold_io.py docstring, E2):
  - market_id: dense 0-BASED per-day ints (MARKET_ID_BASE, matching the
    stream_seq convention), minted in first-appearance order over the
    merged stream; DAY-SCOPED per §2.1 — same-day 1:1 market_id<->ticker
    enforced at write time AND re-checked at reader open (defense in
    depth); any violation = loud "BUILD FAILURE" GoldIOError
  - the reader is constructed per (root, date) so every access carries a
    date; cross_day() is the ONLY cross-day helper and requires a
    keyword-only market_ticker string — no market_id form exists, so
    market_id-only cross-day joins are structurally impossible (test
    demonstrates the same ticker minting DIFFERENT ids on two days)
  - manifest written LAST; carries md5 of the gold .bin AND every sidecar,
    record/trade/market counts, markets_missing_dim (missing-dim markets
    are kept with empty fields, never dropped — V10 spirit), builder
    version, source-day ids, V5/V7 "pending" verdict placeholders
  - reader refuses on: missing manifest, manifest not listing required
    files, missing listed file, any md5 mismatch, record-count/file-size
    disagreement (truncation with doctored md5s still refused, D2),
    sidecar bijection violation
  - taker_side encoding: yes=1 no=2 0=none; trade payload zero unless
    TRADE; trade_id_hash = fnv1a64(full UUID); reconcile_trade_hashes()
    (V8 shape) cross-checks every trade + flags missing/orphan sidecar
    rows; nlevels > uint16 = loud build failure (W2.2 BACKLOG resolved)
- acceptance demonstrated: suite RED first (ImportError: gold_io), then 27
  tests green; V11 runtime half = 10,020 records, mmap random access ==
  streamed parse on 1,000 sampled + all-rows equality; 4 mutants each
  turned their fixture red (dup-ticker check dropped, dup-id check
  dropped, reader md5 verify disabled, hash reconciliation disabled) and
  full green after restore; full pytest 132 passed; make check ALL PASS;
  run_pipeline.sh == PIPELINE PASS == (test_gold_io wired); check_registry
  ok (86 tools)
- rollback: revert 7aebacb + delete work/gold/ (derived, rebuildable)
- next: W2.5 validator harness (gold_validate.py: V2,V3,V4,V6,V9,V10 +
  quarantine; one seeded-defect gold file per check — the io writer can
  now produce them)

---

## 2026-07-06 — W2.3 DONE (merge iterator, TDD red-first, mutation-proven)

- commits: 73f4df8 (W2.3: tools/gold_merge.py + tests/test_gold_merge.py +
  4 merge defect fixtures + registry wiring + 3 BACKLOG notes)
- decisions (rationale in tools/gold_merge.py docstring, E2):
  - type_priority: TRADE=0, ALL other kinds=1. §2.2 mandates TRADE <
    BOOK_DELTA at equal ts_us; extended to snapshot/L1 (conservative: a
    trade is never credited with same-µs state that may postdate it);
    non-trade kinds deliberately share one priority — distinct priorities
    would reorder same-µs events INSIDE one source, breaking file order
  - source_file_order = (source_index, position); a "source" is any
    file-ordered event list; equal-key ties exhaust the lower-indexed
    source's run first (deterministic)
  - fail-closed precondition (S2): each source non-decreasing in (ts_us,
    type_priority) or GoldMergeError "Merge Order Violation" — W2.1's
    reported-not-fixed ts regressions therefore fail the merge; the
    disposition policy is W2.5/W2.6's (BACKLOG)
  - stream_seq is 0-BASED dense (documented choice, STREAM_SEQ_BASE)
  - book_seq minted (last+1) exactly on FSM APPLIED/INVALIDATED —
    invalidation IS a state mutation — identical by construction to W2.2
    book_version and enforced by an FSM-oracle replay test; book_seq 0 =
    "no book state ever emitted"; trades carry the market's current
    book_seq (pre-trade book at equal µs)
  - v3_violations/v6_violations read ONLY emitted records — W2.5 reuse
- context capsule: 24 tests green (105 whole scaffold); acceptance
  demonstrated: structural V6 on synthetic streams (every TRADE:
  ts(mint) <= ts(trade) AND merge-pos(mint) < merge-pos(trade), asserted
  by module checker AND independent in-test re-derivation AND FSM-oracle
  replay). Anti-fake-green: priority-inversion mutant => 6 red; book_seq
  mint+2 gap mutant => 13 red; no-mint-on-invalidation (duplicate) mutant
  => 5 red; restored green each time; v3/v6 checkers proven red on
  doctored records. run_pipeline PIPELINE PASS incl. new test_gold_merge
  suite; make check ALL PASS; registry 85 tools ok. gold_merge.py is 197
  physical lines incl. docstring (<= ~300).
- blocked / handoff: next fresh session runs the independent audit of
  W2.3, then W2.4 (gold writer/reader). W2.6 composition notes filed in
  BACKLOG: per-(channel, market) source slicing (warehouse load() orders
  by market, ts), ts-regression disposition policy, per-record
  fsm.state() recompute perf, HEARTBEAT row-mapping (merge side proven).
  GoldRecord layout untouched (frozen).

## 2026-07-06 16:30 UTC — W2.2 DONE (book FSM, TDD red-first, mutation-proven)

- commits: c5a2e9a (W2.2: tools/gold_fsm.py + tests/test_gold_fsm.py + 6 FSM
  defect fixtures + registry wiring + 2 BACKLOG notes)
- decisions (rationale in tools/gold_fsm.py docstring, E2):
  - invalidation CLEARS levels (hpp keeps them but refuses accessors —
    clearing gives identical observable zeros with a stronger
    no-resurrection bound); state() serves zeroed arrays + F_BOOK_VALID=0
  - every invalidation (Sequence Gap OR negative delta) counts as
    "Resync Required" — mirrors orderbook.hpp, which requests a resync on
    corruption exactly as on a gap
  - while invalid, deltas are refused BEFORE gap detection: a gap check is
    meaningless without a baseline; only a snapshot resets state + seq
  - coverage vs validity split: covered-but-invalid keeps F_BOOK_COVERED
    (subscription fact) while dropping F_BOOK_VALID (state fact); L1-only
    markets serve slot-0 top-of-book with F_BOOK_VALID=1, F_BOOK_COVERED=0,
    empty-side sentinels (bid 0 / ask 10000) zero their side
  - F_FROM_SNAPSHOT set by snapshot (and L1 is_snapshot rows), cleared by
    the first applied delta; heartbeats/trades create no book entry at all
- context capsule: 30 tests green (81 whole scaffold); acceptance
  demonstrated: negative-delta => INVALID no-clamp, invalid-until-snapshot,
  revalidation from later snapshot, permanent invalid without one, V9
  heartbeat neutrality, crossed flagged-not-repaired, yes-space transform
  (ask = 10000 - no_price), DEPTH-tail rest aggregation, both seq modes
  (seq_unavailable stated pre-W5). Anti-fake-green: clamp mutant => 9 red,
  latch-neuter mutant => 11 red, restored green both times. run_pipeline
  PIPELINE PASS incl. new test_gold_fsm suite; make check ALL PASS;
  registry 84 tools ok. FSM is 250 physical lines incl. docstring (≤~300).
- blocked / handoff: next fresh session runs the independent audit of W2.2,
  then W2.3 (merge iterator, pure logic on synthetic lists). HEARTBEAT
  row-mapping and uint16 nlevels write-gate filed in docs/BACKLOG.md for
  W2.3/W2.6 and W2.4. GoldRecord layout untouched (frozen).

## 2026-07-06 UTC — W2.1 DONE (typed loaders, TDD red-first, full-day real demo)

- commits: 05b203d (W2.1: tools/gold_load.py + tests/test_gold_load.py +
  golden/defect fixtures + registry wiring)
- decisions:
  - empty-side L1 encodings pinned from measured staging data: no bid =>
    (0, qty 0), no ask => (10000, qty 0); trade yes+no price == 10000 exactly
    -> gates in tools/gold_load.py (rationale in module docstring)
  - float detection without a `float` token (grep gate applies to the loader
    itself): type-name check + _FloatToken str-subclass sentinel for JSON
    numeric tokens — a float is never constructed from input, only rejected
  - string numbers parse as DOLLARS (integer digit accumulation -> E4);
    ints pass through as already-E4 (staging is typed) — both proven
    byte-exact round-trip on real golden rows (V1)
  - ts monotonicity/same-µs clusters tracked PER MARKET (load() orders by
    market_ticker, ts_utc — a global tracker false-counts every market
    boundary); report-only, never reordered
- context capsule: golden rows sampled from real 2026-07-06 staging OUTSIDE
  08:18–08:36 UTC (G5, padded; window noted in
  tests/fixtures/gold_golden_rows/README.md). Full-day demo through the
  gates: trades 1,180,038 rows -> 4,710 quarantined (4,676 duplicate
  trade_ids + nulls + 1 out-of-range + 4 bad pairs), 100% inside the splice
  window with ZERO date logic; l1 5.71M rows -> 12 quarantined; full 103k
  clean. Operator outputs at work/gold/loader_report_20260706_w21_demo.json
  (+ quarantine/, malformed_record_sample). Anti-fake-green: dedupe and
  float-grep gates each demonstrated red by mutation, restored green.
  44 tests; run_pipeline PIPELINE PASS incl. new test_gold_load suite;
  registry 83 tools ok. Staging-dup observation filed in docs/BACKLOG.md
  (possible W5 rider — ingest.py is W5-only).
- blocked / handoff: next fresh session runs the independent audit of W2.1,
  then W2.2 (book FSM, pure, zero I/O) per PLAN_GOLD_DATA_CONTRACT.
  GoldRecord layout untouched (frozen). Loader code lines: 303 effective
  (412 physical incl. docstrings) — within the ~300 size discipline.

## 2026-07-06 15:44 UTC — W1 DONE (implement -> audit FAIL -> red-first fix -> re-audit PASS)

- commits: 7a69329 (W1: GoldRecord 512B layout contract, C++/Python parity,
  gitignore fixture negation G2, TDD red-first), 17adaee (audit F1 fix:
  FNV-1a-64 offset basis had dropped its final digit; 3 known-vector parity
  tests added to BOTH languages, red-first)
- decisions:
  - FNV constants written in hex in both languages so they are character-
    identical -> include/trading/gold_record.hpp + tools/gold_dtype.py
  - numpy endian-test semantics ('<' canonicalizes to '=' on LE) -> corrected
    equivalent-strength assertion in tests/test_gold_dtype.py
- context capsule: independent audit (subagent, separate context) FAILED W1
  first pass on a real cross-language defect: C++ FNV basis constant
  1469598103934665603 (true: 14695981039346656037 = 0xCBF29CE484222325) —
  every hash diverged from Python, zero tests existed on either side. The
  anti-fake-green protocol caught exactly what it was designed to catch.
  Re-audit verified: vectors 0xcbf29ce484222325 (''), 0xaf63dc4c8601ec8c
  ('a'), 0x2b7e2a9e8505c3a4 (uuid) green both sides, make check ALL PASS,
  registry 82 tools. GoldRecord layout freeze now in effect for W2+.
- blocked / handoff: next session executes W2.1 (typed loaders) per
  PLAN_GOLD_DATA_CONTRACT @ 07c682d with R's blanket approval already given
  (2026-07-06 "全部批准"); golden-row fixtures must avoid the 08:18-08:35 UTC
  corrupted window (G5); one W per fresh session.
## 2026-07-06 15:24 UTC — R adjudication merged (07c682d); WP-00 executed and green

- commits: 07c682d (gold plan: R decisions G1-G8 merged; R-side patches for
  G3/G6/G7/G1/G2/G5/G8 + audit-side V5 support-size and never-prune list),
  WP-00 commit (BACKLOG, pytest.ini, tests/conftest.py, tests/run_pytest.sh,
  make test, tools.json run_pytest entry)
- decisions:
  - 342a114 commit-hygiene violation acknowledged: git add -A swept in
    operator plan edits + pipeline-churned config CSVs -> explicit-path adds
    from now on; churn issue -> docs/BACKLOG.md for R
  - pytest scaffold policy -> pytest.ini + tests/conftest.py (legacy suites
    canonical under make check until WP-04 migrates; empty collection = green
    via exit-5 mapping in tests/run_pytest.sh)
- context capsule: WP-00 DoD all green (make test 0-tests exit 0; registry 80
  tools; make check unbroken). Gold plan on disk now carries all 8 R
  decisions; FNV-1a-64 chosen for trade_id_hash; GoldRecord arithmetic
  512B via _reserved[5]. W1 remains BLOCKED on explicit operator approval.
- blocked / handoff: awaiting R approval to start W1. Nothing else in flight.
## 2026-07-06 14:09 UTC — Gold contract plan audited: APPROVED, 8 findings (3 MUST)

- commits: (this commit) docs/plan_audits/2026-07-06_gold_data_contract.md
- decisions:
  - Audit verdict + findings -> docs/plan_audits/2026-07-06_gold_data_contract.md
  - MUST-fix before W1: (G1) W1 blocked on WP-00 pytest scaffold; (G2)
    .gitignore negation for tests/fixtures/** + git-tracked assertion, else
    all gold fixtures are silently swallowed by global *.ndjson/*.csv.gz
    ignores; (G3) GoldRecord sizeof arithmetic is 488 not 512 — fix is
    _reserved[5] (40B) to land exactly on 512
- context capsule: offsets verified field-by-field (identity 0-32 incl
  book_seq, trade 32-56, arrays 56-440, tail 440-488). V5 day-one support is
  only 4 full-depth markets in a ~4h window — report must print support size.
  trade_id_hash algo is an OPEN QUESTION for R (xxhash=new dep; FNV-1a-64
  recommended, V8 collision check makes weakness detectable). V12 needs a
  7-day staleness bound. work/gold needs local retention (keep N days).
- blocked / handoff: R adjudicates G6 (hash algo) + confirms G3 fix; then
  WP-00 -> W1. Queue pointer unchanged (EXECUTION_PLAN WP-00 first).
## 2026-07-06 12:57 UTC — v1.0 verbatim recovered; EXECUTION_PLAN upgraded to v1.2 (all GAPs resolved)

- commits: (this commit) v1.0 verbatim preservation + v1.2 merge + Cowork
  session's CLAUDE.md exit-ritual + SESSION_LOG.md institutionalization
- decisions:
  - v1.0 original text → `docs/plan_audits/2026-07-06_EXECUTION_PLAN_v1.0_original.md` (verbatim, per handoff)
  - EXECUTION_PLAN.md → v1.2: v1.0 content merged with audit corrections
    C-A/C-B + amendments A1-A6; all 11 v1.1 [GAP]s resolved; PREVENT rule
    and quality_log schema retained
- context capsule: WP dependency spine restored (WP-01→04→{06,08,09};
  WP-03 early; WP-05 parallel; WP-02 closed done-prior). WP-06 wiggle
  formula pinned by fixture: mids [10,12,10,12] → K=12, z=2, wiggle=4.
  WP-09 gate fixture: 3-leg bracket asks sum 96c → BUY signal, gross 4c.
  S-B sealed content: World A/B merge → fill-sim calibration → tanh
  inventory + log-odds skew. Pipeline live: staging ~1M L1 rows day 1;
  first archive tonight UTC midnight; earliest gate 2026-07-13 (H-3).
- blocked / handoff: WP-00 is the next executable WP (BACKLOG.md + pytest
  scaffold + make test + quality_log). Nothing else blocked.
## 2026-07-06 (Cowork session) — SESSION_LOG + exit ritual institutionalized

- commits: pending — next repo session must include these files in its commit
- decisions:
  - Session exit ritual (commit + log entry + queue pointer) → `CLAUDE.md`
  - Current-queue pointer (EXECUTION_PLAN.md → PLAN_GOLD_DATA_CONTRACT.md) → `CLAUDE.md`
- blocked / handoff:
  - v1.0 original text still lives in a Claude Code session's context;
    operator has the prompt to write it verbatim to
    `docs/plan_audits/2026-07-06_EXECUTION_PLAN_v1.0_original.md` and fill
    all v1.1 [GAP]s from it. Do this BEFORE that session is closed.
  - Calendar: 48h capacity observation follow-up; 2026-07-13 earliest H-3
    seven-clean-days gate (Branch A vs maker adjudication happens at gate,
    same-change MM_ROADMAP update per audit A6).

## 2026-07-06 (Claude Code session) — rescue + EXECUTION_PLAN v1.1 reconstruction

- commits: ac2d2ad (batch rescue: 77 uncommitted items — pipeline code,
  GUARDRAILS, roadmap, audits, plans; secrets/data verified clean),
  c316329 (EXECUTION_PLAN.md v1.1-RECONSTRUCTED from audit only + PREVENT
  rule: audits must cite repo path + git hash, nonexistent path = reject)
- decisions:
  - quality_log schema defined → `docs/EXECUTION_PLAN.md` (WP-00 scope)
  - audit-protocol rule f → `docs/EXECUTION_PLAN.md`
- blocked / handoff: 11 [GAP] markers in v1.1 (see that session's GAP table);
  v1.0 verbatim text survives in that session's context — recoverable.

## 2026-07-06 (Cowork session) — codebase review, coverage audit, gold contract

- commits: included in ac2d2ad rescue batch
- decisions:
  - Gold Standard data contract (GoldRecord 512B, FSM/merge/validators
    V1–V16, W2 split into 6 atomic sub-steps with must-fail seeded-defect
    fixtures) → `docs/PLAN_GOLD_DATA_CONTRACT.md`
  - Coverage facts (day one): L1 13,455 mkts / traded 5,692 / full-depth 4;
    4,332 traded-no-L1 = Class B by design (4,134 Exotics/MVE); Sports fully
    Class A, 13 subcats present → recorded in the plan §0
- blocked / handoff:
  - Depth expansion (orderbook_delta for high/mid-liquidity + all sports) is
    design+probe only (W6), rollout needs its own operator-approved plan
  - Class B promotion policy decision deferred: per-market vs wholesale —
    operator decides after a week of promotion-candidate reports
  - work/ hygiene approved but not executed: metrics.ndjson rotation, stray
    log cleanup, raw gzip, archive off-box copy

## Before 2026-07-06 — prehistory (reconstructed)

34 commits, 8191062..020deef (2026-07-05): WS engine passes P0–P8, token/
rate-limit system T0–T6, PLAN_LIVE_VALIDATION P0–P3. Data pipeline went 24/7
on 2026-07-06. Note: until ac2d2ad, the entire Python pipeline layer had
never been committed — the failure class this log exists to prevent.
