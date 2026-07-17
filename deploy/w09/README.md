# PIPE-W09 software bring-up

This bundle installs the isolated W09 research reader on the already-created
`r8g.2xlarge` instance. It does not create or resize AWS resources, change IAM,
touch the production EC2 host, publish to S3, or start Track A.

## Fixed contract

- Instance: `i-0e53d134dceffe166`, Ubuntu 24.04 arm64, `us-east-2`.
- Role: `w09-research-runner`; IMDSv2 temporary credentials only.
- S3: versionless reads only for `research/releases/*/MANIFEST.json`, then
  exact `GetObjectVersion` reads of eligibility-tagged canonical objects.
  There is no write code path.
- DuckDB: `1.4.5`, matching production.
- Cache: `/srv/w09-research/cache` on the 300 GB gp3 root volume.
- Time: UTC with chrony using Amazon Time Sync.
- Shutdown: after 1,800 seconds with no SSH and no `w09-run` inhibitor.
- W09 contains neither the Mac private key nor Kalshi/AWS static credentials.

The dual v2/v3 reader is taken only from the clean recovery repository.  The
complete import set (`research_data.py`, `research_reference.py`, and
`warehouse_common.py`) is pinned by `research_reader_modules.sha256`, checked
before packaging, checked again on the host, and installed non-executable with
mode `0644`.  The W09 wrapper adds IMDSv2/session-token signing without adding
any S3 operation.

The general selector remains copied-v2 by default for rollback compatibility.
The acceptance script is deliberately a separate v3 canary path: it uses an
isolated `cache-v3-canary`, passes `--require-v3-reference`, refuses copied-v2
even when that v2 release has a newer data date or publication time,
and never asks to fetch RFQ. Installing this bundle alone does not publish a
v3 manifest or enable canonical IAM.

## 1. Confirm stop-not-terminate, then install

Before arming automatic shutdown, confirm in the EC2 instance details that
**Shutdown behavior = Stop**. If the AWS CLI is available to the operator, the
equivalent read-only check is:

```bash
aws ec2 describe-instance-attribute \
  --region us-east-2 \
  --instance-id i-0e53d134dceffe166 \
  --attribute instanceInitiatedShutdownBehavior \
  --query 'InstanceInitiatedShutdownBehavior.Value' --output text
```

The result must be exactly `stop`. Then run from the Mac:

```bash
cd "/Users/ritcardo/HFT BOT"
W09_SHUTDOWN_BEHAVIOR_CONFIRMED=stop bash deploy/w09/push_and_install.sh
```

The installer validates the instance ID, region, architecture and exact role
before writing anything. It proves that the idle guard sees both the
provisioning marker and the live SSH connection before it arms the timer.

## 2. Prove the real 30-minute shutdown

Close every SSH, SFTP and ControlMaster connection to `18.226.151.192`. Do not
use a shortened timeout. From the first idle timer observation, the instance
requests poweroff after at least 1,800 seconds (normally within 30–32 minutes).
Observe the EC2 state transition `running -> stopping -> stopped`, then start
the same instance again. No role or network change is needed.

This real stop/restart is intentionally not self-restarted: the instance role
has no EC2 mutation permission. That is part of the isolation boundary.

## 3. Run W09 acceptance after restart

Within 30 minutes of restart:

```bash
cd "/Users/ritcardo/HFT BOT"
W09_CONTROL_PLANE_STOP_OBSERVED=stopped bash deploy/w09/run_acceptance.sh
```

Acceptance fails closed unless it finds exactly one prior idle-poweroff event
with `idle_for_sec >= 1800` and a different boot ID. It then uses the instance
profile to run `inventory`, explicitly selects the newest v3 reference by
**data date** (same-date ties by publisher time), fetches its exact VersionIds
with RFQ OFF, and runs explicit `verify`. Success requires
`REFERENCE_V3/CANONICAL_REFERENCE` and ends with `W09_READY`.

`W09_READY` does not authorize research. Track A remains held until the
separate `W05_ACCEPTED` operator gate exists.

Copied-v2 rollback is explicit: restore the prior broad `research/*` reader
policy (after review), use `/srv/w09-research/cache`, and run the selector
with neither `--require-v3-reference` nor `--include-v3-reference`. Never keep
the broad v2 policy attached while claiming the v3 least-privilege boundary
is active.

## Workloads and rollback

Detached jobs must use:

```bash
w09-run command args...
```

`w09-run` uses a narrowly scoped sudo helper only to acquire the shutdown
inhibitor; the requested workload is then executed as `ubuntu`, never as root.

Emergency disable (keeps the machine running):

```bash
sudo touch /etc/w09-idle.disabled
sudo systemctl mask --now w09-idle-check.timer
```

The current On-Demand cost contract is stored at `/etc/w09/cost-contract.json`:
compute `$0.4713/hour` while running; with 300 GB gp3 and one public IPv4 the
730-hour effective running rate is about `$0.50918/hour`. Stopping removes the
compute line but storage and the Elastic IP remain billable.
