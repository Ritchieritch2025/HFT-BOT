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
- Bounded `.08` envelope: 8 vCPUs / 64 GiB physical RAM, DuckDB `16GB`
  with 2 threads, systemd `MemoryHigh=40G` and `MemoryMax=52G`, and one
  24-hour service deadline.
- Cache: `/srv/w09-research/cache` on the 300 GB gp3 root volume.
- Time: UTC with chrony using Amazon Time Sync.
- Shutdown: after 1,800 seconds with no SSH and no `w09-run` inhibitor.
- W09 contains neither the Mac private key nor Kalshi/AWS static credentials.

The dual v2/v3 reader and Deep03 runtime are taken from the same clean exact
release commit. The packager refuses a separate source worktree unless its
HEAD is exactly the release HEAD. The
complete import set (`research_data.py`, `research_reference.py`, and
`warehouse_common.py`) is pinned by `research_reader_modules.sha256`, checked
before packaging, checked again on the host, and installed non-executable with
mode `0644`.  The W09 wrapper adds IMDSv2/session-token signing without adding
any S3 operation.

The bounded Deep03 D3-W2A open-discovery payload is a separate five-module
set pinned by `deep03_open_discovery_modules.sha256`. The installer checks it
before copying any module and installs three argument-preserving wrappers:
`deep03-v3-w1-preflight`, `deep03-v3-prepare`, and `deep03-v3-run`. The W1
preflight command records the exact input, data-quality, prior-exposure and
no-holdout receipts without network access or research computation. The
prepare command requires one or
more explicit V3 release IDs and refuses copied-v2, `latest` selection, RFQ,
static AWS credentials, trading credentials, marker drift and non-content-
addressed cache files. The runner has no network client and produces only
`EXPLORATORY_ONLY` descriptive D3-W2A artifacts.

The general selector remains copied-v2 by default for rollback compatibility.
The acceptance script is deliberately a separate v3 canary path: it uses an
isolated `cache-v3-canary`, passes `--require-v3-reference`, refuses copied-v2
even when that v2 release has a newer data date or publication time,
and never asks to fetch RFQ. After exact-version `verify`, acceptance runs the
SHA-256-pinned `v3_query_canary.py`. That reader calls the canonical v3
manifest validator, requires the verified marker and active-view provenance to
name the same date/release, and makes DuckDB execute `DESCRIBE` plus `LIMIT 1`
against one manifest-bound `local_key` for every facts table. Its atomic JSON
receipt contains source/version/query/schema/row hashes, not sampled values.
Installing this bundle alone does not publish a v3 manifest or enable
canonical IAM.

There are two deliberately non-interchangeable query gates. `w09-accept` is
strict acceptance and still requires `SEALED_CONFIRMATION`.
`w09-exploratory-autoresearch.service` is explicitly
`MODE 1 / EXPLORATORY_AUTORESEARCH`; it accepts either
`SEALED_DEGRADED_EVIDENCE` or `SEALED_CONFIRMATION`, always brands results as
exploratory and not strict acceptance, and refuses every RFQ-bearing manifest.

The exploratory timer inventories v3 manifests, requires a contiguous
RFQ-free date window beginning at 2026-07-10, selects one frozen release ID per
date, fetches and verifies exact VersionIds through the W09 instance profile,
rebuilds the view with the explicit `--include-non-confirmation` switch, runs a
manifest-bound DuckDB canary for every release, then executes the bounded
Deep03 D3-W2A discovery runner. Selection, failure, canary, and completion
receipts live below `/srv/w09-research/automation`; one selection digest can
produce only one successful research completion. The 30-minute persistent
timer is a retry path, not a duplicate-run path.

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

The installer validates the instance ID, exact `r8g.2xlarge` type, 8 online
vCPUs, at least 60 GiB visible RAM, region, architecture and exact role before
writing anything. It proves that the idle guard sees both the
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
`REFERENCE_V3/CANONICAL_REFERENCE`, `SEALED_CONFIRMATION`, RFQ OFF, an exact
`view/.view_provenance.json` date-to-release binding, and a passing real
DuckDB query receipt. Only then does it end with `W09_READY`.

The query receipt is written under
`/srv/w09-research/acceptance/v3-query-canary-<release-id>.json`. A failed
rerun atomically replaces an older green receipt with a `REFUSED` receipt, so
the operational artifact cannot silently retain a stale PASS.

`W09_READY` does not authorize research. Track A remains held until the
separate `W05_ACCEPTED` operator gate exists.

## 4. Run one explicit D3-W2A open-discovery cycle

After acceptance, inventory, fetch and verify the exact V3 releases for every
date in the first research window: **2026-07-10 through 2026-07-17 inclusive**.
Copy each exact release ID from its verified cache directory; do not invoke a
newest/latest selector in the research command and do not omit a date merely
to reduce local I/O. Keep both preparation and execution under the shutdown
inhibitor:

```bash
RUN_ID="d3-w2a-open-$(date -u +%Y%m%dT%H%M%SZ)"
RID_0710="2026-07-10__v3ref__seal-EXACT8__pub-EXACT16"
RID_0711="2026-07-11__v3ref__seal-EXACT8__pub-EXACT16"
RID_0712="2026-07-12__v3ref__seal-EXACT8__pub-EXACT16"
RID_0713="2026-07-13__v3ref__seal-EXACT8__pub-EXACT16"
RID_0714="2026-07-14__v3ref__seal-EXACT8__pub-EXACT16"
RID_0715="2026-07-15__v3ref__seal-EXACT8__pub-EXACT16"
RID_0716="2026-07-16__v3ref__seal-EXACT8__pub-EXACT16"
RID_0717="2026-07-17__v3ref__seal-EXACT8__pub-EXACT16"

w09-run deep03-v3-prepare \
  --cache /srv/w09-research/cache-v3-canary \
  --run-root /srv/w09-research/runs \
  --run-id "$RUN_ID" \
  --release "$RID_0710" \
  --release "$RID_0711" \
  --release "$RID_0712" \
  --release "$RID_0713" \
  --release "$RID_0714" \
  --release "$RID_0715" \
  --release "$RID_0716" \
  --release "$RID_0717"

w09-run deep03-v3-run \
  --run-dir "/srv/w09-research/runs/$RUN_ID"
```

For multiple explicit dates, repeat `--release EXACT_RELEASE_ID`. The first
run is the complete eight-day window, not a capacity sample. For the currently
published set, `INPUT_MANIFEST.json` should recompute 8 dates, 2,657 candidate
objects and 29,473,216,651 bytes from the exact manifests; these values are an
operator cross-check, never hard-coded input defaults. Any difference must be
explained from the exact release ledger before interpreting results. Success
is only `RUN_COMPLETE.json` written last after `INPUT_MANIFEST.json`, quality,
estimability, exclusions, per-method receipts, `RESULTS.json`, the self-
contained `REPORT/index.html`, and `ARTIFACT_SHA256SUMS`. B01–B04 each close as
`EXECUTED` or `NOT_ESTIMABLE` with evidence. This run never reads RFQ and never
emits a strategy-PnL, confirmation, promotion, shadow or order claim.

## 5. Automatic MODE 1 operation

The full installer installs but deliberately leaves the automatic timer
disabled. The audit-candidate plan is PLAN-ONLY and supplies no execution
authority. A future exact-SHA D3-W2A release must first name the authority,
write root, input, cost and end/stopping rule. Only then may the operator
upgrade the currently installed W09 from this clean release worktree with:

```bash
cd /Users/ritcardo/HFT-BOT-tagger-release
W09_SOURCE_REPO=/Users/ritcardo/HFT-BOT-tagger-release \
W09_SHUTDOWN_BEHAVIOR_CONFIRMED=stop \
  bash deploy/w09/push_and_install.sh
```

The normal installer creates none of the three execution-gate files. That
future release must install all of these as root-owned mode `0444` files:

- `/etc/w09/deep03/adopted-plan.md`, whose exact SHA is named by authority;
- `/etc/w09/deep03/AUTHORITY.json`, binding verbatim operator text, adopted
  plan SHA, installed source commit, D3-W2A/OPEN_DISCOVERY, exact v3 release
  IDs, fixed W09 write roots, positive spend cap, runtime/expiry, RFQ OFF, and
  every mutation/credential/order/Telegram boolean explicitly false;
- `/etc/w09/deep03/approvals/d3-w2a-execution-arm.json`, binding the byte SHA
  of that authority, the same release and source commit, and a narrower active
  UTC window.

`deep03_authority_gate.py` validates all three plus
`/opt/w09/research/release-commit.txt` before every service cycle. The consumer
validates them again and refuses if its selected release list differs by even
one ID. A missing, expired, writable, linked, SHA-mismatched, generic, or
zero-spend authority leaves the service skipped/refused; the installer never
fabricates a passing example.

After that separate release and the reviewed role policy both exist, the
operator may explicitly enable the timer and start an immediate cycle:

```bash
ssh -i ~/.ssh/kalshi-key.pem ubuntu@18.226.151.192 \
  'sudo systemctl enable --now w09-exploratory-autoresearch.timer; sudo systemctl start w09-exploratory-autoresearch.service'
ssh -i ~/.ssh/kalshi-key.pem ubuntu@18.226.151.192 \
  'systemctl status --no-pager w09-exploratory-autoresearch.service; cat /srv/w09-research/automation/status.json'
```

The service contains no S3 write operation, no static AWS or trading
credential, and no `--with-rfq` path. A root-owned executable at
`/opt/w09/research/hooks/after_exploratory_autoresearch` may optionally consume
the completed immutable run bundle. Absence of that hook is normal because
Deep03 itself is the default research payload.

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
