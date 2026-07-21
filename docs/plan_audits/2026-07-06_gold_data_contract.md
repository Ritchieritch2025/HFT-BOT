# Plan Audit: PLAN_GOLD_DATA_CONTRACT.md

**Audited document:** `docs/PLAN_GOLD_DATA_CONTRACT.md` @ `ac2d2ad` (371
lines; pasted W-section verified identical to the committed file). PREVENT
rule satisfied.

**Verdict: APPROVED WITH 8 FINDINGS** — GUARDRAILS §6: 10/10 pass. This is
the best-engineered plan reviewed to date: the anti-fake-green rule (every
check must have a seeded-defect fixture that makes it RED) mechanizes D2
beyond what GUARDRAILS itself requires; per-W allowed/forbidden write lists
implement least-privilege; V5's "widening δ to absorb mismatches is
FORBIDDEN" and V10's coverage honesty are exactly the house failure class,
pre-armed. W5 (the one production-adjacent change) carries D4 same-commit
tests, an explicit P4 continuity statement, and checkpoint-safe rollback.

## §6 checklist
1 phase named (1.5, feeds 1.5-C): pass · 2 live orders: none (W6 probe
operator-gated, read-only): pass · 3-4 strategy math: n/a · 5 WS trading
path: n/a (World A/B explicitly out of scope) · 6 tests: EXCEEDS (red-fixture
rule) · 7 pipeline continuity: pass (per-W forbidden writes + W5 statement) ·
8 reversible/bounded: pass (per-W rollback, ≤300 lines, one W = one commit) ·
9 docs same change: pass (W5+schema doc) · 10 green-can't-lie: pass (V5/V7/
V10 design).

## Findings (binding where marked MUST)

**G1 (MUST) — W1 is blocked on WP-00.** The gold workstreams assume pytest
scaffolding + `make test` exist; those are WP-00 deliverables
(EXECUTION_PLAN v1.2). CLAUDE.md's queue already orders them — make the
dependency explicit: W1 starts after WP-00 is green. Amendment A1 (one test
infrastructure) binds the gold suites: each new suite gets a tools.json entry
+ pass token + run_pipeline.sh inclusion (W1's allowed writes already permit
this — use them).

**G2 (MUST) — .gitignore will silently swallow every fixture.** Root
`.gitignore` globally ignores `*.ndjson`, `*.parquet`, `*.csv.gz`.
`tests/fixtures/gold_golden_rows/*`, `gold_defects/*`, and W3.3's
`kalshi_golden/*` (real captured frames) will look committed locally but
never enter the repo — CI green on the author's machine, broken on every
fresh clone. This is the plan's own target failure class (silent loss under
green). Fix in W1: append negation `!tests/fixtures/**` (+ re-include
subpatterns as needed) to .gitignore, and add a repo-hygiene assertion to the
suite: every fixture file referenced by a test is `git ls-files`-tracked.

**G3 (MUST) — the GoldRecord arithmetic does not reach 512.** Field-by-field
offsets: identity block ends at 32 (the "(32 B)" comment includes book_seq;
fine), trade block ends at 56, book arrays 56→440, rest/nlevels/pads
440→472, `_reserved[2]` 472→488. **sizeof == 488, and
`static_assert(sizeof(GoldRecord)==512)` fails as written.** Fix before W1:
`_reserved[5]` (40 B) lands exactly on 512, preserving zero-copy alignment
(all int64 fields stay 8-aligned). W1 would have caught this red — catching
it in audit saves the stumble and proves the layout-test-first design right.

**G4 — V5/W3.1 day-one support is 4 markets.** Grounding facts: full-depth
coverage = 4 watchlist leftovers, overlapping L1 only inside the ~4h
watchlist window. The δ distribution is valid but thin; the W3.1 report MUST
print its support size (markets × hours) so a 4-market δ is never read as
global truth (D2). Support widens when W6's depth expansion lands.

**G5 — golden rows must avoid the corrupted window.** W2.1/W3.3 fixtures
sampled from real 2026-07-06 data must be drawn outside 08:18–08:35 UTC
(double-writer splice window; corrupt staging rows were purged but raw shards
retain spliced lines) and note the sampling window in a fixture README.

**G6 (OPEN QUESTION for R) — trade_id_hash algorithm.** xxh64 is not in the
Python stdlib and this machine takes no new deps without operator approval
(no brew; pip gated). Options: (a) approve `xxhash` pip install both for
Python and vendor XXH64 in C++; (b) stdlib-only `blake2b(digest_size=8)`
(Python) + a small vendored BLAKE2b or a different C++-side impl; (c)
FNV-1a-64 — trivial to implement identically in both languages, weak alone
but V8 asserts zero collisions against the sidecar every build, which makes
weakness detectable. Recommend (c) for zero-dependency parity; R decides.

**G7 — V12 staleness bound.** "kalshi_spec_sync green (last saved result)"
needs a max-age: saved result older than 7 days ⇒ re-run with
--allow-network before the gold build, else the gate can pass on a stale
snapshot.

**G8 — work/gold retention.** Derived and rebuildable (correctly excluded
from raw retention), but unbounded daily .bin growth on a laptop disk needs a
line: keep last N days locally, older rebuilt on demand (matches D1: archive
is truth, gold is derived).

## Sequencing note
Queue remains: WP-00 → (EXECUTION_PLAN WPs and W1→W5 may interleave after
WP-00, respecting each plan's own spine) → W6 operator-gated. W5 and WP-03/
WP-08 touch adjacent territory (freshness/quality vs exporter) — no file
conflicts found; keep one-W-one-commit discipline and conflicts stay
impossible.
