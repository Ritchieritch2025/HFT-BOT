# Plan Audit: System Discovery & Ground-Truth Report (2026-07-06)

Verdict: **APPROVED WITH 7 AMENDMENTS** (GUARDRAILS §6: 10/10 pass, none rejected;
amendments close gaps found during audit). Phase mapping: Phase-2 pre-work
(World A/B merge design input); satisfies P1 once Amendment 1 lands.

## §6 checklist result
1 phase-named: pass w/ A1 · 2 live-order safety: pass (demo-only; see A2) ·
3 log-odds+fees: n/a (discovery) · 4 pessimistic bound: n/a · 5 WS-path: pass
(is its purpose) · 6 tests: pass w/ A3 · 7 pipeline continuity: pass w/ A4-A5 ·
8 reversible+bounded: pass w/ A6 · 9 docs: pass (output is a doc + CORRECTIONS
list) · 10 can't-lie-green: pass (evidence tags are the plan's best feature).

## Amendments (binding)

**A1 — Name the phase.** Header must state: "Phase-2 pre-work; feeds the World
A/B merge design; no gate is satisfied or skipped by this mission."

**A2 — Pre-register the demo reality.** Our engine rejects `demo` fail-closed
by design (env.cpp resolve_runtime; exchange_check.sh: "Kalshi's demo exchange
is unavailable and unsupported"). Expected outcome: Part 1.5 resolves to
DOCS-ONLY for order lifecycle behavior. Any demo probing happens via throwaway
scripts in sandbox/discovery/ hitting demo endpoints directly — NEVER by
changing our engine's env gates (that would violate READ-ONLY and weaken S2).
Real order-lifecycle verification is Phase-4 work (1c post-only via the
existing preflight --order, operator-confirmed). Do not creatively work around
this. Addition: subscribing to PRIVATE WS channels (fills/order-status) on prod
transmits nothing — subscribe/auth/heartbeat semantics MAY be [VERIFIED-LIVE];
fill *delivery* semantics stay [DOCS-ONLY] until Phase 4.

**A3 — Exception-class fixes ship with tests.** The "silent-data-loss bug may
be fixed immediately" exception inherits D4/E1: fix + regression test in the
same change, logged in the report.

**A4 — Measurement isolation.** (a) Item 7 book-lag: prefer replay-based
measurement (captured recv_mono_ns + bench_ws_decode apply times) — zero touch
on production. If a live end-to-end number is required, run a SEPARATE
ws_shadow instance with its OWN capture path in sandbox — never append to the
production hourly logs (double-writer incident class). (b) Item 8 DuckDB
second-writer experiments run against a COPY of staging.duckdb in sandbox,
never the live file. (c) Item 12 REST probes: harmless GETs, bounded count,
spaced — they share the account's read bucket with nothing critical, but log
the token cost.

**A5 — Build on, don't re-derive.** Part 2 starts from
docs/ARCHITECTURE_REVIEW_2026-07-06.md (already [VERIFIED-CODE] by independent
audit) and verifies/deltas it, rather than re-inventorying from scratch. The
14-item gap register in that doc is the Part-3 input. Ready [VERIFIED-LIVE]
sources that already exist: account_info (tier/limits/costs), spec-sync
snapshots in docs/vendor/kalshi, bench_rtt output (36.3ms p50 measured
2026-07-06), lifecycle/status artifacts.

**A6 — Budget and stop-rule.** Discovery missions rabbit-hole. Cap: if a Part
exceeds its expectation materially (e.g., docs contradict live in >3 places),
stop and surface rather than expanding scope. Sandbox scripts are deleted
after the report is ACCEPTED (not after the run) — outputs preserved in the
appendix (E2).

**A7 — Part 6 expectation-setting.** As of 2026-07-06 there are ZERO completed
archived days (day 1 in progress; first export tonight UTC midnight). Clean-day
count will be 0-1; the report must state this plainly rather than presenting
day-1 calibration observations as anything more than observations (Q4).

## Pre-answered facts the executor should carry in
- Corrupted window 2026-07-06 08:18–08:35 UTC: purged from staging, raw shards
  retain spliced lines, ingester validates (report in remediation status).
- Staging freshness incident (rotation shards) fixed same day with widened
  glob; regression risk class noted in D4.
- Python 3.9.x system interpreter (EOL Oct 2025) — already a known debt item.
