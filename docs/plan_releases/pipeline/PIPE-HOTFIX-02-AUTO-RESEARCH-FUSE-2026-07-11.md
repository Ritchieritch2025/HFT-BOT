# PIPE-HOTFIX-02 — production auto-research fuse (2026-07-11)

**Status:** INDEPENDENT_AUDIT_PASS_AWAITING_DEPLOYMENT

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
