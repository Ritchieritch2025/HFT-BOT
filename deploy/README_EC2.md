# deploy/ — EC2 pipeline host (W-A1, PLAN_AWS_MIGRATION)

**Box:** i-0fd427becf740a06b · r8g.large (2 vCPU/16 GB, Graviton4) · Ubuntu
24.04 arm64 · us-east-2 · 200 GB gp3 (W-A0 option 1, operator-picked
2026-07-08). Repo lives at `/home/ubuntu/hft-bot`; venv at `.venv`.

## Files

| file | role |
|---|---|
| `kalshi-pipeline.service` | systemd replacement for the Mac LaunchAgent; Restart=on-failure ONLY (app watchdog owns reconnect); installed DRY until W-A4 |
| `kalshi-oom-guard.{service,timer}` + `oom_guard.sh` | one-box isolation (16 GB): ws_shadow pinned -1000 (never OOM-killed), batch python +300/renice+10/ionice-7 (first sacrificed, CPU/IO-deprioritized) |
| `bringup_ec2.sh` | idempotent bring-up: deps → UTC → chrony(Amazon Time Sync) → 16 GB swap → venv (duckdb==1.4.5) → `make -j` → units installed dry |
| `SECURITY_CHECKLIST_EC2.md` | operator console steps: SSH-from-my-IP, egress 443-only lockdown (post-bring-up), verification commands |
| `com.ritcardo.kalshi-pipeline.plist` | the Mac LaunchAgent (stays installed-but-dormant on the Mac after cutover = rollback host) |
| `bootstrap.sh`, `tradingd.service`, `tradingd.env.example` | LEGACY (tradingd era, pre-pipeline). Kept for reference; not used by W-A1 |

## How code reaches the box (no GitHub credentials on the box — S4)

The GitHub remote needs auth; the box gets NONE of it. The Mac pushes over
SSH instead:

    # once, on the box:      git init --bare ~/kalshi.git
    # once, on the Mac:      git remote add ec2 ubuntu@<EC2-IP>:kalshi.git
    # every update, Mac:     git push ec2 <branch>
    # box:                   git clone ~/kalshi.git ~/hft-bot   (first time)
    #                        cd ~/hft-bot && git pull            (updates)

## The three load-bearing launchd behaviors (W-A1 acceptance)

1. **Hourly raw-log rotation** — inside `tools/pipeline_supervisor.sh`
   (KALSHI_SHADOW_SECONDS to the hour boundary; loop respawns). systemd does
   not own rotation; nothing to configure.
2. **Single-instance lock** — inside the supervisor
   (`work/live/supervisor.lock`). A second instance exits 0; with
   `Restart=on-failure` systemd does not respawn-loop on it.
3. **App owns reconnect** (W-C1 ladder + STEP 0 re-signing) — the unit
   deliberately has NO `WatchdogSec=`; `Restart=on-failure` only, RestartSec
   30 s mirrors launchd ThrottleInterval.

Exit 78 (EX_CONFIG, `~/.kalshi/env.sh` missing) intentionally does NOT
restart (`RestartPreventExitStatus=78`) — visible in `systemctl status`
instead of a silent 30 s crash loop.

## What W-A1 must NOT do

No capture start (W-A4 cutover, operator present), no `~/.kalshi/env.sh`
(operator hand-creates, 600), no S3/IAM (W-A3), no edits to
capture/ingest/export source.
