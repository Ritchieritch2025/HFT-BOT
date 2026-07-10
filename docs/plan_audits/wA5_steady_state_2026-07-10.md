# W-A5 — post-cutover steady state — validation log (2026-07-09/10)

**Verdict: ✅ all machinery built, deployed, and individually verified;
⏳ two acceptance items are TIME/OPERATOR-gated and stay open** — the 24 h
zero-gap window (closes ≥2026-07-10 23:09 UTC; restart moved the clean-start
to 00:09:48Z, so measure 00:10→next 00:10) and the operator-channel items
(Telegram token, CloudWatch/SNS, first bill / budget cap, Mac sleep
re-enable which is gated on the 24 h verdict). Nothing blocks capture.

## ① Final Mac→S3 delta — DONE

Mac-only residual (raw date=2026-07-09 hours 17–23, incl. the 17:00→18:31
window only the Mac captured) vaulted with the same three-hop discipline:
49 files / 12 GB → `HOP1 mac_delta BAD=0` → S3 `VERIFY: 49 objects checked,
0 mismatches` (`~/wA5_steady.log` on the box). Vault now holds the COMPLETE
Mac capture history through unload (23:09:07Z). Manifest:
`mac-vault/meta/…delta_1723.txt`-class artifacts + local work/vault/.

## ② EC2→S3 continuous sync — DONE, first runs proven

`deploy/ec2_s3_sync.sh` + systemd timers: hourly :05 (raw completed hours —
current-hour GLOB excluded per the W-A3 torn-copy lesson — + reports/),
daily 03:10 UTC (adds the warehouse durable layer). Never `--delete`;
single-part⇒ETag==MD5. **First hourly run fired on schedule and completed:
journalctl `[ec2_s3_sync] hourly sync complete 2026-07-10T00:05:16Z
(excluded hour 00)`.** EC2 data lands under `s3://kalshi-vault-ritcardo/ec2/`
(provenance-separated from `mac-vault/`).

## ③ Detectors + alerting (rider e) + report flow-back — DONE (Telegram token pending operator)

- `deploy/alert_notify.sh` + kalshi-alert.timer (every 60 s, enabled, first
  runs green-silent as designed): capture_alert.json status / raw freshness
  (>120 s) / disk (>80%) → `work/live/alerts.log` (dashboard stream) +
  Telegram POST once token+chat_id land in env.sh (S4 operator; log-only
  with a LOUD "UNCONFIGURED" marker until then, D2). Anti-spam:
  notify-on-state-change + 30-min re-alert. **5 offline dry-run tests (D4).**
- Report flow-back VERIFIED END-TO-END: EC2 `reports/` placeholder → Mac
  launchd job (`com.ritcardo.kalshi-report-pull` + `mac_report_pull.sh`) →
  **file landed in `/Users/ritcardo/Desktop/TradingSys Report/`** (55 B test
  artifact, 19:59 EDT). macOS TCC caveat: background runs may need the
  operator to grant Full Disk Access to /bin/bash; two-stage design (inbox
  always works + Desktop copy degrades to WARN) makes reports late-never-lost
  either way. CloudWatch/SNS infra layer = operator console (steps handed).

## ④ Cost budget — measured side done; bill + cap = PENDING OPERATOR

Run-rate from prices/measurements: r8g.large $86 + 200 GB gp3 $16 + IPv4 $4
+ S3 (~100 GB vaulted now ≈ $2.3, ec2/raw accrues ~25–30 GB/day ≈ +$0.6/mo
PER DAY of history — ~+$20/mo each month of operation) ≈ **$110/mo today,
rising with S3 raw history**. Levers noted for the cost review: Glacier
transition for aged raw (NOT deletion — allowed by the no-delete rules),
or a raw-retention ruling for S3. Proposed budget alarm: **$150/mo** (AWS
Budgets, operator console). First real bill figure: operator to paste.

## ⑤ Mac sleep re-enable — DEFERRED BY DESIGN

Gated on the 24 h zero-gap verdict (plan text). Until then
`pmset disablesleep 1` stays. Command for the operator after the 24 h check
passes: `sudo pmset -a disablesleep 0`.

## ⑥ Riders — DONE

- **(a) metrics rotation**: `tools/rotate_metrics.sh` (512 MB default,
  env-tunable, keep-3) wired into the supervisor's hourly loop between
  capture segments; semantics + wiring pytest-covered. Applies to both boxes
  by code (Mac dormant).
- **(b) full-market L1**: `config/market_classes.yaml` → ALL 18 categories
  class_a_full_l1 (Exotics' Q7 MM-exclusion is research-side, unchanged);
  contract test pins the policy; coverage-audit policy test updated; live on
  EC2 since the 00:09:48Z restart (next classification cycle promotes
  series_classified). Raw backfill of pre-promotion Class-B L1 remains
  possible from the vaulted raw (playbook 3e's fuller variant — BACKLOG).
- **(c) churned CSVs gitignored** (untracked + .gitignore; box conflict from
  the last churn resolved).

## ⑦ vault_staging reclaim — DONE (operator-approved)

92 GB / 1,132 files deleted (counts logged pre-deletion, P6); disk 65%→**19%**
(157 GB free). The S3 vault (three-hop verified + restore-proven) and the Mac
originals remain.

## ⑧ ticker-conflation fact — DONE

`config/kalshi_facts.yaml` ws.ticker_conflation, VERIFIED-MEASURED with the
W-A4 evidence and the Q5 consequence spelled out.

## ⑨ Audit items

- **SIGTERM graceful stop — root-caused in two layers, fixed in two places,
  honestly PARTIAL:** (1) supervisor bash resumed its loop after the old
  combined trap → split traps (`exit 143` on INT/TERM) + ws_shadow
  backgrounded behind an interruptible `wait` (contract-tested); the
  supervisor now exits <1 s (the 00:09 restart's kill list no longer
  contains it). (2) Children can legitimately outlive 30 s — ws_shadow's own
  graceful drain and python-inside-DuckDB (signals deferred during C calls;
  the restart landed mid-midnight-export) — so `TimeoutStopSec=90` gives
  them runway; SIGKILL remains the backstop. Restart discipline: avoid
  00:00–00:15 UTC (export window). Export self-healing confirmed live:
  write-once refused the partial re-export, the 02:00 `--force` sweep
  completes the day.
- **events/markets 80k cap — WORSE than the audit thought, and productive:**
  BOTH crawls had been truncating at 80,000 EVERY hourly run (the "(capped)"
  marker screamed into an unread log — D2 lesson recorded). Caps raised to
  2000 pages: markets now crawls fully (~50 k open); **events hit the new
  400,000-row cap too — the endpoint returns all-history; pagination is
  newest-first so the cap trims historical tail only.** Interim: 400 k/hour
  is tolerable (paced, ~2 min); proper fix = status-filtered hourly crawl +
  scheduled full crawl, queued to BACKLOG with an E4 spec-check (never
  assume the status param from memory). **The raise itself exposed a latent
  bug caught and fixed same-session:** head-only JSON schema sampling
  crashed on microsecond timestamps at row 363,172 → `sample_size=-1`
  (mixed-format columns fall back to VARCHAR, lossless for the raw dim
  store) + mixed-precision regression test.

## no-fabrication 溯源表

| Number | Source |
|---|---|
| Delta 49 files/12 GB, BAD=0, 49/49 S3 | `~/wA5_steady.log` (box): HOP1/SYNCED/VERIFY lines; du of mac_delta |
| Hourly sync first run 00:05:16Z | journalctl -u kalshi-s3-sync-hourly (quoted) |
| Restart stop behavior | journalctl kill list 00:09:48 (quoted in-session): supervisor ABSENT (exited clean), 4 children SIGKILLed at exactly +30 s |
| Disk 65%→19%, 92 GB/1,132 files deleted | df + du + find -wc pre-deletion, `~/wA5_steady.log` |
| Desktop landing | `ls Desktop/TradingSys Report/flowback_test_20260709.txt` (55 B) + `/tmp/kalshi-report-pull.log` OK line |
| events 400,000 (capped) / crash at row 363,172 | box catalog.log (quoted); the "2025-12-14T12:19:15.983002Z" literal from the DuckDB error |
| All test counts | run_pipeline PIPELINE PASS on BOTH boxes at the final code (Mac exit 0, box exit 0); suites include the 5 alert + 8 pacing/cap + 2 schema-drift + rotation/SIGTERM/rider-b contract tests |
| Alert timer/oom timer/sync timers active | systemctl list-timers output (quoted in-session) |

## Open items at session close

1. ⏳ 24 h zero-gap acceptance: run `capture_gaps --date 2026-07-10` (and
   00:10→00:10 window) after 2026-07-10 23:09Z; then ⑤ Mac sleep re-enable.
2. ⏳ Operator: Telegram token+chat_id → env.sh; CloudWatch StatusCheckFailed
   →SNS→email confirm; first bill figure + $150 budget alarm; (optional) TCC
   Full-Disk-Access for /bin/bash if the scheduled 09:00 pull WARNs.
3. BACKLOG: events crawl redesign (status-filtered hourly + full-crawl
   schedule, E4 spec-check first); 3e-style L1 backfill from vaulted raw;
   rider (d) auto-deploy loop (plan §rider d — not in this W's operator
   scope); Glacier/retention ruling for S3 ec2/raw accrual.
