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
| `/etc/w09/c1/C1_SPEC.md` | `root:root 0444` |
| `/etc/w09/c1/C1_CONFIG.json` | `root:root 0444` |
| `/opt/w09/research/c1-release-commit.txt` | `root:root 0444`; one exact 40-char commit plus newline |
| `/opt/w09/research/tools/research/c1_real_fill_runner.py` | `root:root 0444`; authority-hashed |
| `/opt/w09/research/tools/research/c1_fill_kernel.py` | `root:root 0444`; authority-hashed |
| `/opt/w09/research/tools/research/c1_checkpoint_reader.py` | `root:root 0444`; authority-hashed |
| `/opt/w09/research/deploy/w09/c1_authority_gate.py` | `root:root 0555`; authority-hashed |
| `/usr/local/sbin/c1-run-once` | `root:root 0555`; authority-hashed audited `c1_run_once.sh` |

Every immutable read uses `O_NOFOLLOW`, requires a regular file, and checks the
exact owner/mode before hashing. Create the one-shot receipt directory once as
`root:root 0750`:

```sh
install -d -o root -g root -m 0750 /var/lib/w09-c1/one-shot
install -d -o root -g root -m 0755 /srv/w09-research/c1-runs
```

Do not change the existing `ubuntu:ubuntu 0775` `/srv/w09-research/runs`;
Deep03 owns it. C1 writes only the independent `c1-runs` root.

Do not chmod, copy, move, or create a persistent host bind mount over the
existing Deep03 checkpoint tree.
Its manifests remain owned by the existing `ubuntu` producer. The gate hashes
their exact bytes and rejects group/world-writable manifests. The analyzer is
then dropped to `nobody:nogroup` with only supplementary gid `1000` (the fixed
`ubuntu` reader group); the wrapper proves that this identity can read but
cannot write the full namespace before consuming the ARM. Inside a private,
non-propagating mount namespace it bind-remounts the checkpoint root read-only.
That mount disappears with the process and leaves no host mount behind.

The five exact manifest paths are:

```text
<checkpoint-root>/l2_availability/MANIFEST.json
<checkpoint-root>/l2_replay/MANIFEST.json
<checkpoint-root>/l2_episodes/MANIFEST.json
<checkpoint-root>/trades_market/MANIFEST.json
<checkpoint-root>/dim_market_date/MANIFEST.json
```

## Authority contract

The authority is strict-schema JSON: unknown or missing fields fail closed. A
release process may render it only after the spec, config, final Git commit and
all five checkpoint manifests are frozen. The gate never renders it.

```json
{
  "schema_version": "c1-w09-execution-authority-v1",
  "state": "ACTIVE",
  "release_id": "c1-<release-id>",
  "authorized_instance_id": "i-0e53d134dceffe166",
  "authorized_instance_type": "r8g.2xlarge",
  "authorized_role": "w09-research-runner",
  "operator_authorization_text": "<verbatim text naming release_id and exact commit>",
  "operator_authorization_sha256": "<sha256 of UTF-8 text, without added newline>",
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

The cap must also cover the runtime at the pinned W09 rate of USD 0.50918/hour.

The ARM is also strict-schema JSON and binds the SHA-256 of the authority's
literal bytes—not a re-serialization. Its active window must be strictly
narrower than the authority window.

```json
{
  "schema_version": "c1-w09-execution-arm-v1",
  "state": "ARMED",
  "release_id": "c1-<same-release-id>",
  "authority_sha256": "<sha256 of exact AUTHORITY.json bytes>",
  "exact_git_commit": "<same commit>",
  "run_id": "c1-<unique-run-id>",
  "arm_nonce": "<32-128 URL-safe random characters>",
  "armed_at_utc": "<UTC within authority>",
  "not_before_utc": "<UTC at/after armed_at>",
  "expires_at_utc": "<UTC before/at authority expiry>"
}
```

## Preflight and execution

From the separately controlled root deployment session, first validate without
consuming:

```sh
/opt/w09/venv/bin/python /opt/w09/research/deploy/w09/c1_authority_gate.py validate
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

1. Attest the real instance ID, type, and role through IMDSv2 without fetching credentials.
2. Prove the full checkpoint tree has no group/world-writable node and that `nobody:nogroup` plus only gid `1000` can read but cannot write it.
3. Validate authority, ARM, commit, five installed runtime-file hashes, spec, config and five manifest hashes.
4. Create `/var/lib/w09-c1/one-shot/<ARM-SHA256>.json` with `O_EXCL` before creating any run/output directory or starting computation.
5. Create root-owned `<run>/control`, then persist 0444 `CONTROL_AUTHORIZATION_RECEIPT.json` and `ARM_CONSUMPTION_RECEIPT.json` there; each carries the authority/ARM hashes, commit, spec/config hashes, five runtime hashes, both fixed upstream hashes, five manifest hashes and run ID.
6. Keep `<run>` root-owned and non-writable to the analyzer. Create only `<run>/work` and `<run>/work/tmp` as `nobody:nogroup 0750`, leave `<run>/work/results` absent for the runner, and establish a private non-propagating mount/network namespace.
7. Bind-remount the checkpoint root read-only inside that private namespace, drop permanently to `nobody:nogroup` plus gid `1000`, and invoke the exact runner under the authority/ARM-bounded timeout (never above four hours).

Any retry with the same ARM fails because its receipt already exists, including
when the first attempt failed after consumption. A new attempt needs a newly
approved authority/ARM or, if the unchanged authority remains valid, at least a
new narrower ARM with a new nonce and run ID. Never delete a consumption
receipt to retry.

`c1_authority_gate.py consume` exists for the wrapper and audited service
integration. Running it by hand irreversibly burns the ARM and does not launch
the analysis.

## Expected output boundary

The research process runs with only supplementary gid `1000`, an isolated empty
network namespace, no AWS credentials, disabled IMDS, and a run-local
`HOME`/`TMPDIR`. It can write only `<run>/work`; it cannot unlink or replace the
root-controlled evidence under `<run>/control`. The checkpoint mount is
kernel-enforced read-only. The wrapper
does not create authority, does not call `sudo`, does not install a timer, and
does not start itself after installation.
