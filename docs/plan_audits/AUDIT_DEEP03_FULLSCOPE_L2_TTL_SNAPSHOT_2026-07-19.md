# Independent adversarial audit — DEEP03 full-scope L2 TTL/snapshot hardening

**VERDICT: PASS**

- Audited runtime commit: `1fa748160f532f283dc8e395c36d7133ff8616f9`
  ("Bound L2 dwell to staleness TTL registry and harden snapshot/audit gates")
- Worktree: `/Users/ritcardo/HFT-BOT-deep03-fullscope-09`, branch
  `w-deep03-fullscope-09`, HEAD `65c974b82be8607082efe9eb21e058ae8acb79eb`
- `git diff --stat 1fa7481..HEAD` = exactly one file,
  `docs/plan_audits/AUDIT_FRESH_RFQ_DAILY_PIPELINE_C_2026-07-19.md` (+188).
  Docs-only claim **verified**; no code/tests/deploy touched after the
  audited commit. Worktree clean (`git status --short` empty).
- Auditor: independent adversarial session, 2026-07-19. I did not write the
  code under audit; all findings below were verified by executing the code,
  not by reading the implementer's claims.

## Environment verification

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_deep03*.py`:
  **122 passed** in 9.29s (matches the claimed 122/122; count observed from
  the pytest summary line, not trusted from the commit message).
- `shasum -a 256 -c deploy/w09/deep03_open_discovery_modules.sha256`: all 8
  modules OK, including the changed `deep03_v3_methods.py`,
  `deep03_fullscope_graph.py`, `deep03_v3_l2.py`,
  `deep03_fullscope_runner.py`.
- `shasum -a 256 -c deploy/w09/exploratory_autoresearch_payload.sha256`: all OK.
- duckdb 1.4.5 used for all ad-hoc replays.

## Real production quality objects (P0 check)

Source: `/private/tmp/claude-501/-Users-ritcardo-HFT-BOT/ec9683c0-3454-4978-bf11-1785f94da169/scratchpad/l2_quality/l2_gaps_2026-07-{12..17}.json`.
SHA-256 of 7/12 recomputed =
`b7d9aa3764c903872bc487924e324f18917131d6fb5349d96b0e4af651a9c7cf` — matches
the production record.

I fed all six REAL receipts to the runtime gate
(`assess_l2_quality_receipt`, `tools/research/deep03_v3_l2.py:326`) plus the
inclusion rule at `deep03_v3_l2.py:1963`:

| date | gate state | included | blockers observed |
|---|---|---|---|
| 2026-07-12 | PASS | yes | none |
| 2026-07-13 | REFUSED | no | `seq_gap_events=1`, `seq_missed_total=8` |
| 2026-07-14 | REFUSED | no | `parse_errors=26881`, `seq_gap_events=123490`, `seq_missed_total=1.12e25`, `seq_regressions=120740`, `markers_lost_frames=11420` |
| 2026-07-15 | PASS | yes | none |
| 2026-07-16 | REFUSED | no | `recorder_marker:epoch_change=3`, `recorder_marker:gap=364` |
| 2026-07-17 | PASS | yes | none |

Exactly the handoff's date policy: {12,15,17} clean, {13,14,16} excluded, and
the gate consumes the real objects without schema failure (7/14's
oversized `seq_missed_total` integer is handled and blocks). The clean days'
`transport_close` recorder markers correctly do not block the date — reconnects
are handled per-market by the snapshot-censoring engine, not by date exclusion.
**No misclassification. P0 check passes.**

## Per-finding verdicts (handoff "Worktree A" items 1–7)

**1. Staleness TTL — CLOSED (verified by attack).**
`STALE_TTL_REGISTRY_NS = (250ms, 1s, 5s)`, `PRIMARY_STALE_TTL_NS = 1s`
(`deep03_v3_l2.py:62-63`); `_l2_abi` refuses a primary TTL outside the
registry (`:921`). Dwell is `least(next_clock-recv, ttl)` per TTL stratum via
`CROSS JOIN (VALUES ...)` (`:1275-1297`), with reason
`RIGHT_CENSORED_STALE_TTL` when the gap exceeds the TTL. Fail-closed
post-check: `ttl_dwell_violations` (`:1622-1633`) rejects any stratum with
`p95_dwell_us > ttl` or `total_dwell_us > n_rows*ttl`. Attack (a): synthetic
same-epoch interval with the next update **4 hours** later contributed
exactly 250 000 / 1 000 000 / 5 000 000 µs in the three strata, each labeled
`RIGHT_CENSORED_STALE_TTL` — no unlimited uptime possible.

**2. Snapshot distinction — CLOSED (verified by attack).**
Engine at `deep03_v3_l2.py:527-585`. Same-sid strictly-increasing-seq over a
valid epoch: horizons that provably passed expire first (`_expire`, `:567`),
remaining episodes censored at the snapshot clock
(`right_censored_snapshot_boundary`), snapshot never counted as refill
(episodes are popped before `book.snapshot`; `refill_ns` stays None).
New-sid/reconnect: censors at the **last proven valid market clock**
(`right_censored_reconnect_snapshot`, `:571-576`) — my 4-hour-later reconnect
snapshot produced 0 ns of extra observation. Duplicate/equal-seq and
regressed same-sid snapshots: `REJECTED_SEQUENCE_REGRESSION`, censored at
last valid clock (`:549-562`). Malformed: `REJECTED_INVALID_SNAPSHOT`,
censored at last valid clock (`:535-547`). Atlas side: a reconnect snapshot
interval is `SNAPSHOT_RESET` with **zero** dwell (verified).

**3. Adversarial tests — CLOSED.**
`tests/test_deep03_v3_l2.py` contains dedicated tests for every demanded
class: legal same-sid snapshot (`:251`), exact one-second boundary (`:275`,
`:294`, `:405`, `:421`, `:675`), delayed new-sid reconnect (`:232`),
duplicate (`:313`), regressed (`:153`), malformed (`:336`), TTL registry cap
(`:627`), continuous-snapshot dwell (`:722`). All run and pass in the 122.

**4. Narrow labeling, no market-uptime claim — CLOSED (verified by grep + ABI).**
`DWELL_SEMANTICS = "TTL_CAPPED_UPDATE_TO_UPDATE_DWELL_NOT_MARKET_UPTIME"`
(`deep03_v3_l2.py:64`) propagated to the ABI (`state_dwell.semantics`,
`unbounded_uptime_from_late_next_update: False`,
`pause_close_terminal_lifecycle_boundaries_proven: False`, `:946-953`), to
atlas receipt metrics (`market_uptime_claim: False`, `:1680`), to
`limitations` (`:2322-2323`), and to the HTML report (`:857-861` of the
runner). The runner **enforces** the label: `_validate_l2_result` refuses any
execution receipt whose `dwell_semantics` differs
(`deep03_fullscope_runner.py:461-471`). Source grep found no affirmative
uptime claim anywhere — every occurrence is a negation.

**5. Standalone `build_market_graph` row authority — CLOSED (verified by attack).**
`deep03_fullscope_graph.py:279` uses `type(declared) is not int or declared < 0`
— attack (e): `row_count=True` (bool) **raised**, `2.0` (float) **raised**,
missing/None, `-2`, and `"2"` all **raised** `MarketGraphError`; exact
`row_count=2` passed; typed-but-wrong `3` vs actual 2 raised the conservation
error. (`type(x) is int` correctly rejects bool since `bool` is a subclass.)

**6. Audit gate binds `deep03_v3_methods.py` SHA + new blocker classes — CLOSED (verified by attack).**
`FULLSCOPE_SOURCE_MODULES` includes `deep03_v3_methods.py`
(`deep03_fullscope_runner.py:94-102`); `_validate_l2_independent_audit`
(`:264-342`) requires `audited_modules_sha256` to equal the **live**
recomputed hashes. Attack (f): I copied `tools/research` to an isolated dir,
flipped one byte in `deep03_v3_methods.py`, and re-ran the gate with a receipt
built against the pristine hashes — refused with
`L2 independent audit gate mismatch: audited_modules_sha256`. Non-empty
`blockers` refused; removing `SNAPSHOT_REGRESSION` from
`blocker_classes_checked` refused. `L2_AUDIT_BLOCKER_CLASSES` (`:117-131`)
contains `STALENESS_TTL`, `SNAPSHOT_REGRESSION`, `RESET_BEFORE_EXPIRY`,
`MANIFEST_ROW_AUTHORITY` (plus the prior nine). The repo test
`test_audit_gate_binds_methods_module_and_new_blocker_classes`
(`tests/test_deep03_fullscope_runner.py:568`) additionally proves refusal
happens before any compute.

**7. Real hazard/dwell/coverage/balance/concentration tables — CLOSED (verified structurally + by attack).**
`_l2_report_tables` (`deep03_fullscope_runner.py:641-798`) reads the
**validated durable parquet partitions** (`_validated_stage_partitions`
re-validates receipt SHA, manifest binding, canonical JSON, and
payload-inside-root before any read) and computes `refill_hazard` and
`state_dwell` by SQL over the atlas parquet; match coverage/balance/
concentration come from the match-partition receipts whose metrics were
computed from `l2_match_build` at write time (`deep03_v3_l2.py:1761-1903`,
including SMDs from `var_pop`, HHI/max-share concentration). Attack (g):
synthetic 3-episode cohort (refills at 150 ms, 450 ms, one censored) —
hazard table produced the exact discrete risk sets (3→2→1), events in the
right 100 ms buckets, and product-limit final survival
(1−1/3)(1−1/2)=0.3333 — a genuine estimator, not a raw fraction and not
hardcoded. The report render (`_render_fullscope_report`) fails closed if
its L2 sections are absent.

## Attack log (all attacks failed to produce a wrong number)

| # | attack | result |
|---|---|---|
| a | next update 4 h later, all three TTL strata | dwell capped at exactly one TTL per stratum, `RIGHT_CENSORED_STALE_TTL`; conservation held (3 rows per stratum) |
| b | same-sid snapshot with seq regression (and equal-seq duplicate) | `REJECTED_SEQUENCE_REGRESSION`; episode censored at last valid clock (0 ns extra), never continuous, never refill |
| b3 | new-sid snapshot 4 h later over open episode | `right_censored_reconnect_snapshot` at last valid clock; atlas `SNAPSHOT_RESET` interval accrued 0 dwell |
| c | same-sid snapshot at exactly depletion+1 s / +1 ns | exact: `right_censored_snapshot_boundary` (dur = 1 s, no event); +1 ns: horizon expiry fires **first** (`right_censored_1s_horizon`, end = boundary). Ordering as claimed |
| c2 | refill delta at exactly +1 s / +1 ns | exact: observed event (closed endpoint per ABI `endpoint_rule`); +1 ns: censored non-event — `_expire` runs before the refill check (`deep03_v3_l2.py:626`) |
| d | smuggle uptime claim | none found; label enforced by `_validate_l2_result` |
| e | `build_market_graph` with `row_count` True / 2.0 / None / −2 / "2" / wrong int | all refused; exact match accepted |
| f | one-byte tamper of `deep03_v3_methods.py`; non-empty blockers; dropped blocker class | all refused by the gate |
| g | fake-table probe: 3-episode risk-set recomputation | hazard/survival numbers match hand computation from partition data |
| real | 6 real production `l2_gaps` receipts | classification exactly per policy (see table above) |

## Regression / determinism check

- All durable `select_sql` ORDER BY clauses are unique-key total orders:
  replay `(market_ticker,recv_wall_ns,recv_mono_ns,ws_sid,ws_seq)` with a
  pre-replay uniqueness assertion (`_assert_partition_order_unambiguous`,
  `deep03_v3_l2.py:1074`, which also rejects same-key payload variants);
  episodes end with unique `episode_id`; atlas ordering covers every GROUP BY
  key of all five record kinds; matches end with `(episode_id, control_id)`,
  both unique in the final selection. Deterministic.
- Row conservation strengthened, not broken: per-TTL stratum conservation
  (`deep03_v3_l2.py:1693-1704`) requires each TTL copy to independently sum
  to the eligible state rows; episode-atlas, source→replay, classification,
  and included+excluded coverage equations all still enforced and all
  re-validated by `_validate_l2_result` (all four must be PASS with
  observed == expected).

## Notes (non-blocking)

- P2: `_l2_report_tables` line 710 guards the TTL-registry coverage check
  with `if state_dwell and ...` — a totally empty STATE stratum set (possible
  only if zero valid book states exist on every clean date) would pass that
  specific check silently. The `if not atlas: raise` and the row-conservation
  equations still bound this; it cannot alter any emitted number.
- Note: the audit-gate SHA binding is computed at run start and re-checked at
  `deep03_fullscope_runner.py:1069` and `:1196`; the residual import-time vs
  hash-time window is the same pattern already accepted for the base runner
  and is out of scope for the seven findings.
- Reminder (unchanged from handoff): the commit is gate-hardened but the unit
  remains non-runnable until a real `L2_INDEPENDENT_AUDIT_RECEIPT.json` PASS
  bound to this exact commit exists; this report does not itself constitute
  that machine receipt.
