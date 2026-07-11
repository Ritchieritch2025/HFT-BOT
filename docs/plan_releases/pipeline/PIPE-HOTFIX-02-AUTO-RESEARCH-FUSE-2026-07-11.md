# PIPE-HOTFIX-02 — production auto-research fuse (2026-07-11)

**Status:** DEPLOYED_LIVE_IMMEDIATE_GATES_PASS — TWO-HOUR-BOUNDARY SOAK PENDING

Independent audit: PASS, no blocking findings.  Verbatim archive:
`docs/plan_audits/AUDIT_PIPE_HOTFIX_02_2026-07-11.md`.

## Operator authorization (verbatim)

> 你来修复吧

Context: the operator authorized repair after the sealed-day research chain
automatically relaunched `mm_scan` following two manual terminations and again
exhausted the 2-vCPU/16-GiB production data box.

## Incident evidence

- Production base before this hotfix: `f4769ad4cf56`.
- First observed run: PID 21331, approximately 14 GiB RSS; host swap rose to
  approximately 12 GiB before operator termination.
- Automatic retry: PID 27344 on 2026-07-11 16:07 UTC; only about 349 MiB host
  memory remained and approximately 14 GiB swap was in use.
- The retry was terminated at 16:10 UTC.  Raw capture continued growing and
  available memory recovered to approximately 12 GiB.

These measurements are live EC2 observations, not estimates.

## Authorized scope

1. Default `AUTO_RESEARCH=0` in both the supervisor and production systemd
   unit.
2. Preserve seal verification, `capture_gaps`, and `coverage_audit`.
3. Skip only `mm_scan`, `mm_backtest`, and `mm_calibrate` on production.
4. Log `AUTO_RESEARCH_DISABLED` every attempted sealed-day research cycle.
5. Never manufacture a research success receipt.
6. Add a regression contract test, deploy the audited commit, perform one
   short controlled pipeline restart, and verify raw + ingest recovery.

Out of scope: EC2 resize, L2 rollout, trading/live orders, S3/IAM split work,
research-model changes, or fabricated completion state.

## Safety and rollback

- Invalid `AUTO_RESEARCH` values force `0` (fail closed).
- `run_daily_research` still performs current-seal verification and the daily
  coverage audit before the heavy-tool fuse; the quality gates are not lost.
- The production unit pins `AUTO_RESEARCH=0`, so credential-file or inherited
  shell state cannot silently re-enable it.
- Rollback is a code-only revert of this hotfix after stopping the unit.  Do
  **not** roll the repository back to historical commit `a011fab`, which
  predates the deployed W02/TL1 data contracts.

## Deployment acceptance

- exact audited commit present on EC2;
- systemd unit reinstalled and daemon-reloaded;
- controlled restart exits without SIGKILL;
- exactly one `ws_shadow` and one `ingest.py --loop`;
- no `mm_scan`, `mm_backtest`, or `mm_calibrate` across two UTC hour changes;
- startup log contains `auto_research=0` and cycle log contains
  `AUTO_RESEARCH_DISABLED`;
- raw file grows, staging advances, seal alarm remains absent, swap does not
  grow;
- deliberate restart gap is recorded with exact UTC bounds.

## Live deployment receipt

- Audited runtime commit: `e63b771d6a0bef10fc8c563922a67aad661631ad`
  (production fast-forward from `f4769ad4cf56`).
- Long-lived recovery branch equivalent: `bc76fa7`.
- Installed file SHA-256:
  - `tools/pipeline_supervisor.sh` =
    `b414d08ea38dc9c38fbd0b8e7676f10bc50cd864e3f303c9ad74d27d851adb28`
  - `/etc/systemd/system/kalshi-pipeline.service` =
    `93cd61d526b03070800e96d7bf18fd80c0dfb93f722430deb7c0a29984ca33c2`
- `systemd-analyze verify` passed before and after unit installation;
  `systemctl show` reports `AUTO_RESEARCH=0`.
- Controlled restart: `2026-07-11T16:20:01Z`; systemd returned active in the
  same UTC second, with no timeout or SIGKILL.  The first conservatively
  attributable post-boundary raw record had
  `recv_wall_ns=1783786801986330281` (`16:20:01.986330281Z`), so the immediate
  quality-log bracket is under one second.  The sealed-day `capture_gaps`
  reconciliation remains authoritative.
- Live evidence after restart:
  - exactly one `ws_shadow` and one `ingest.py --loop` under the systemd main
    PID; no `mm_scan/mm_backtest/mm_calibrate`;
  - startup log: `auto_research=0`;
  - cycle log: `AUTO_RESEARCH_DISABLED ... seal verification and coverage
    audit completed`;
  - raw grew in repeated five-second samples; staging mtime advanced at
    16:22:52Z and again at 16:23:34Z;
  - no seal alarm or fatal restart event.
- Restart catch-up observation: ingest briefly materialized an approximately
  12.9-GiB backlog batch, then fell to approximately 0.7 GiB; available host
  memory recovered to approximately 13 GiB and swap fell to approximately
  88 MiB.  This is a separate bounded-ingest backlog issue for W03; the three
  fused research tools remained absent throughout.
- Append-only `work/quality_log.ndjson` contains the deliberate-maintenance
  record written at `2026-07-11T16:24:24Z`.

Still pending before final closure: read-only verification after two UTC hour
changes.  EC2 resize, L2, S3/IAM split work and trading remain untouched.
Heartbeat automation `verify-pipe-hotfix-02` is active for exactly two hourly
read-only checks and is instructed to delete itself after final PASS.
