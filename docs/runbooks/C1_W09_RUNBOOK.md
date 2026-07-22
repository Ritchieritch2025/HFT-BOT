# C1 W09 one-shot runbook

## Purpose and non-authority

This runbook executes one offline C1 real-fill kill test on the fixed W09 host.
It does not authorize, create, transmit, amend, or cancel an order. It does not
read RFQ data, mutate production, or write S3. Installing the package does not
start it; there is no timer. The separately approved `AUTHORITY.json` and its
narrower one-shot `EXECUTION_ARM.json` are mandatory.

Fixed execution identity and scope:

- EC2 instance: `i-0e53d134dceffe166`
- instance type: `r8g.2xlarge`
- instance-profile role: `w09-research-runner`
- dates: `2026-07-12`, `2026-07-15`, `2026-07-17`
- checkpoint namespace: `/srv/w09-research/checkpoints/source-c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979`
- sole output root: `/srv/w09-research/c1-runs`, with an ARM-bound `c1-*` run ID
- maximum runtime: 14,400 seconds
- maximum cost authorization: USD 3

## Immutable files

Install the audited exact commit first. Do not overwrite or reuse Deep03's
`release-commit.txt`; C1 has its own commit binding.

| File | Owner/mode |
|---|---|
| `/etc/w09/c1/AUTHORITY.json` | `root:root 0444` |
| `/etc/w09/c1/EXECUTION_ARM.json` | `root:root 0444` |
| `/etc/w09/c1/OPERATOR_APPROVAL.txt` | `root:root 0444`; canonical signed JSON text |
| `/etc/w09/c1/OPERATOR_APPROVAL.txt.sig` | `root:root 0444`; OpenSSH Ed25519 detached signature |
| `/etc/w09/c1/C1_SPEC.md` | `root:root 0444` |
| `/etc/w09/c1/C1_CONFIG.json` | `root:root 0444` |
| `/opt/w09/research/c1-release-commit.txt` | `root:root 0444`; one exact 40-char commit plus newline |
| `/opt/w09/research/tools/research/c1_real_fill_runner.py` | `root:root 0444`; authority-hashed |
| `/opt/w09/research/tools/research/c1_fill_kernel.py` | `root:root 0444`; authority-hashed |
| `/opt/w09/research/tools/research/c1_checkpoint_reader.py` | `root:root 0444`; authority-hashed |
| `/opt/w09/research/deploy/w09/c1_authority_gate.py` | `root:root 0555`; authority-hashed |
| `/usr/local/sbin/c1-run-once` | `root:root 0555`; authority-hashed audited `c1_run_once.sh` |
| `/usr/bin/ssh-keygen` | `root:root 0755`; fixed SHA-256 `621136662bb8552f45bdec675fa676d47d2ba9f258574bc58efeccb813b14bc0` |

Every immutable read uses `O_NOFOLLOW`, requires a regular file, and checks the
exact owner/mode before hashing. Create the one-shot receipt directory once as
`root:root 0750`:

```sh
install -d -o root -g root -m 0750 /var/lib/w09-c1/one-shot
install -d -o root -g root -m 0755 /srv/w09-research/c1-runs
```

Do not change the existing `ubuntu:ubuntu 0775` `/srv/w09-research/runs`;
Deep03 owns it. C1 writes only the independent `c1-runs` root.

Before any ARM consumption, the wrapper resolves the virtual-environment
Python to `/usr/bin/python3.12`, verifies its exact SHA-256
`a7d56a8a764faf7bbf5c164055a48fd072be52287bdeb523a9e07b2042f4e7e1`,
and requires Python `3.12.3`. It verifies DuckDB `1.4.5`, its imported path,
and extension SHA-256
`184620a897f5c1b3dddfa217fe22cd98d614489d8e6bdbfa5fa0b86388af669a`.
The preflight runs with a fixed PATH, isolated Python mode, and Python/loader
injection environment variables removed.

Do not chmod, copy, move, or create a persistent host bind mount over the
existing Deep03 checkpoint tree.
Its manifests remain owned by the existing `ubuntu` producer. The gate hashes
their exact bytes and rejects group/world-writable manifests. The analyzer is
then dropped to `nobody:nogroup` with only supplementary gid `1000` (the fixed
`ubuntu` reader group); the wrapper proves that this identity can read but
cannot write the full namespace before consuming the ARM. Inside a private,
non-propagating mount namespace it bind-remounts all `/srv/w09-research`
read-only, then exposes only `<run>/work` read-write. `/tmp`, `/var/tmp`, and
`/dev/shm` are rebound to run-local directories below `work`. Those mounts
disappear with the process and leave no host mount behind.

The five exact manifest paths are:

```text
<checkpoint-root>/l2_availability/MANIFEST.json
<checkpoint-root>/l2_replay/MANIFEST.json
<checkpoint-root>/l2_episodes/MANIFEST.json
<checkpoint-root>/trades_market/MANIFEST.json
<checkpoint-root>/dim_market_date/MANIFEST.json
```

## Authority contract

The authority is strict-schema JSON: unknown or missing fields fail closed.
The W09 root account is not the approval trust anchor. The gate embeds the
offline operator public key and verifies an OpenSSH Ed25519 signature with
identity `c1-operator-offline-2026-07-22` and namespace
`hft-bot-c1-operator`. The matching private key stays on the trusted Mac and is
never read, printed, committed, or uploaded. This proves W09 cannot locally
self-sign approval; it does not prove a human's biological identity. The Mac
signer and release agent remain trusted components.

After all bytes and exact UTC windows are frozen, the operator returns the
canonical approval text. The trusted Mac signs the exact file:

```sh
ssh-keygen -Y sign -f /Users/ritcardo/.config/hft-bot/c1_operator_ed25519 \
  -n hft-bot-c1-operator OPERATOR_APPROVAL.txt
```

The signed approval is canonical pretty JSON and strictly contains the exact
release, run ID, nonce, authority and ARM windows, instance identity, commit,
three dates, five runtime hashes, two upstream hashes, five checkpoint hashes,
spec/config hashes, roots, runtime/cost bounds and all four false permissions.
The authority embeds the identical text and its external signature SHA.

```json
{
  "schema_version": "c1-w09-execution-authority-v2",
  "state": "ACTIVE",
  "release_id": "c1-<release-id>",
  "authorized_instance_id": "i-0e53d134dceffe166",
  "authorized_instance_type": "r8g.2xlarge",
  "authorized_role": "w09-research-runner",
  "operator_authorization_text": "<exact OPERATOR_APPROVAL.txt bytes represented as a JSON string>",
  "operator_authorization_sha256": "<sha256 of exact OPERATOR_APPROVAL.txt bytes>",
  "operator_approval_signature_sha256": "<sha256 of exact OPERATOR_APPROVAL.txt.sig bytes>",
  "operator_signer_identity": "c1-operator-offline-2026-07-22",
  "operator_signature_namespace": "hft-bot-c1-operator",
  "operator_public_key_fingerprint": "SHA256:AAE/pGNCTrbM2c+kj7CG4QDggGES+YeejuW5Hn593Dg",
  "authorized_run_id": "c1-<unique signed run ID>",
  "authorized_arm_nonce": "<32-128 signed URL-safe characters>",
  "authorized_arm_not_before_utc": "<signed UTC>",
  "authorized_arm_expires_at_utc": "<signed UTC>",
  "exact_git_commit": "<40 lowercase hex>",
  "runtime_file_sha256s": {
    "c1_real_fill_runner.py": "<sha256>",
    "c1_fill_kernel.py": "<sha256>",
    "c1_checkpoint_reader.py": "<sha256>",
    "c1_authority_gate.py": "<sha256>",
    "c1_run_once.sh": "<sha256>"
  },
  "upstream_sha256s": {
    "input_manifest": "<sha256 of fixed prior-run INPUT_MANIFEST.json>",
    "fullscope_l2_execution_receipt": "<sha256 of fixed prior-run FULLSCOPE_L2_EXECUTION_RECEIPT.json>"
  },
  "spec_path": "/etc/w09/c1/C1_SPEC.md",
  "spec_sha256": "<sha256>",
  "config_path": "/etc/w09/c1/C1_CONFIG.json",
  "config_sha256": "<sha256>",
  "checkpoint_root": "/srv/w09-research/checkpoints/source-c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979",
  "checkpoint_manifest_sha256s": {
    "l2_availability": "<sha256>",
    "l2_replay": "<sha256>",
    "l2_episodes": "<sha256>",
    "trades_market": "<sha256>",
    "dim_market_date": "<sha256>"
  },
  "eligible_dates": ["2026-07-12", "2026-07-15", "2026-07-17"],
  "authorized_read_roots": [
    "/etc/w09/c1",
    "/opt/w09/research",
    "/srv/w09-research/checkpoints/source-c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979",
    "/srv/w09-research/runs/mode1-20260710-20260717-3cde714ed188-38f8763d13c0-a1"
  ],
  "authorized_write_root": "/srv/w09-research/c1-runs",
  "run_directory_prefix": "c1-",
  "max_runtime_seconds": 14400,
  "cost_cap_usd": 3.0,
  "live_order_permission": false,
  "production_mutation": false,
  "s3_write_permission": false,
  "rfq_included": false,
  "issued_at_utc": "<UTC>",
  "expires_at_utc": "<UTC, at most 24h after issue>"
}
```

The cap must cover the runtime at the assumed W09 rate of USD 0.50918/hour.
This is an operator-side estimate, not an AWS Budgets or billing API cap; EBS,
network and other charges are not proven by this gate.

The ARM is also strict-schema JSON and binds the SHA-256 of the authority's
literal bytes—not a re-serialization. Its active window must be strictly
narrower than the authority window.

```json
{
  "schema_version": "c1-w09-execution-arm-v2",
  "state": "ARMED",
  "release_id": "c1-<same-release-id>",
  "authority_sha256": "<sha256 of exact AUTHORITY.json bytes>",
  "exact_git_commit": "<same commit>",
  "run_id": "<exact signed authorized_run_id>",
  "arm_nonce": "<exact signed authorized_arm_nonce>",
  "armed_at_utc": "<UTC within authority>",
  "not_before_utc": "<exact signed authorized_arm_not_before_utc>",
  "expires_at_utc": "<exact signed authorized_arm_expires_at_utc>"
}
```

## Preflight and execution

From the separately controlled root deployment session, first validate without
consuming:

```sh
/opt/w09/venv/bin/python -I /opt/w09/research/deploy/w09/c1_authority_gate.py validate
```

Confirm the exact runner CLI before release. The wrapper intentionally keeps
the three runner parameters together and supplies no caller-controlled argv:

```text
--checkpoint-root <fixed checkpoint namespace>
--output-dir /srv/w09-research/c1-runs/<ARM run id>/work/results
--config /etc/w09/c1/C1_CONFIG.json
```

Start once, manually, only after operator approval:

```sh
/usr/local/sbin/c1-run-once
```

The wrapper performs these steps in order:

1. Verify the fixed Python/DuckDB paths, versions and binary hashes in a sanitized environment.
2. Attest the real instance ID, type, and role through IMDSv2 without fetching credentials.
3. Prove the full checkpoint tree has no group/world-writable node and that `nobody:nogroup` plus only gid `1000` can read but cannot write it.
4. Verify the external Ed25519 approval, signed run/nonce/windows, authority, ARM, commit, five installed runtime-file hashes, spec, config and five manifest hashes.
5. Create `/var/lib/w09-c1/one-shot/<SIGNED-APPROVAL-SHA256>.json` with `O_EXCL` before creating any run/output directory or starting computation. The stable signed-scope key rejects a bytewise reserialization of the same approved run/nonce as a second ARM.
6. Create root-owned `<run>/control`, then persist 0444 `CONTROL_AUTHORIZATION_RECEIPT.json` and `ARM_CONSUMPTION_RECEIPT.json` there; each carries the authority/ARM hashes, commit, spec/config hashes, five runtime hashes, both fixed upstream hashes, five manifest hashes and run ID.
7. Keep `<run>` root-owned and non-writable to the analyzer. Create only `<run>/work`, `tmp`, `var-tmp`, and `dev-shm` as `nobody:nogroup 0750`, leave `<run>/work/results` absent, and establish a private non-propagating mount/network namespace.
8. Remount all `/srv/w09-research` read-only, restore only `<run>/work` read-write through a private tmpfs alias, redirect the three global temporary roots into work, recompute remaining seconds from the immutable absolute consumption deadline, drop permanently to `nobody:nogroup` plus gid `1000`, and invoke the exact runner under that shorter timeout.

Any retry with the same ARM fails because its receipt already exists, including
when the first attempt failed after consumption. Because run ID, nonce and both
UTC windows are signed, every retry needs new signed operator approval and a new
authority/ARM. Never delete a consumption receipt to retry.

`c1_authority_gate.py consume` exists for the wrapper and audited service
integration. Running it by hand irreversibly burns the ARM and does not launch
the analysis.

## Expected output boundary

The research process runs with only supplementary gid `1000`, an isolated empty
network namespace, no AWS credentials, disabled IMDS, and a run-local
`HOME`/`TMPDIR`. Within `/srv/w09-research`, it can write only `<run>/work`; it
cannot unlink or replace the root-controlled evidence under `<run>/control`.
This is not a general proof that every path elsewhere on the host is read-only.
The checkpoint mount is kernel-enforced read-only to C1. This does not freeze an independent host-side
`ubuntu` producer outside the namespace; exact per-file checkpoint hashes and
producer quiescence remain the TOCTOU controls. A W09 no-ARM namespace dry-run
on 2026-07-22 proved work writes succeed, production/checkpoint writes fail,
run-local temp redirection succeeds, and no mount remains after exit. The wrapper
does not create authority, does not call `sudo`, does not install a timer, and
does not start itself after installation.
