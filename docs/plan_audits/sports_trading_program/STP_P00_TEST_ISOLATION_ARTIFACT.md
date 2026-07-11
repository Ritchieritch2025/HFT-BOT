# STP_P00_TEST_ISOLATION_ARTIFACT — Section 31.2 test-isolation artifact

- release: STP-R003-TEST-ISOLATION (receipt:
  `docs/plan_releases/sports_trading_program/STP-R003-TEST-ISOLATION.md`,
  operator_text_sha256 `fec0ec9919d6c94abfbabfb2ddac95a7fe6c2a5f7499803aca0b4d62092d00ce`)
- W-id: STP-P00-ISO-W01 (Session 1, implementation)
- date: 2026-07-11 (both proof runs completed 2026-07-11 17:53:59Z / 17:58:16Z;
  mtimes of the two RUN_SUMMARY.txt proof files)
- implementation commit: `0dbeb75ceea4b73689480b631b2fc708d3fb3d46`
  (branch `plan-sports-market-dynamics-v2`, parent = receipt commit `48d41f0`)
- verdict: **✅ PASS — both isolated runs green (66/66 suites), operational
  worktree byte-identical before/after each run, all 11 fail-closed negative
  controls behaved as required.**
- artifact SHA-256: recorded in `STP_P00_TEST_ISOLATION_MANIFEST.json`
  (house convention — the manifest carries this file's hash as of the
  implementation-artifact commit; no self-referencing hash inside this file).

---

## 1. What was built

Three repository changes (full diff = implementation commit `0dbeb75`):

1. **`tests/isolated_run.sh`** (new) — the isolation harness. Runs the
   complete required sequence

       make all
       make check
       tests/run_pipeline.sh
       python3 tools/check_registry.py --require-built

   inside a fresh isolated root under `/private/tmp/stp-p00-test-isolation*`,
   and proves the operational worktree unchanged via complete before/after
   snapshots (§3). Fail-closed preconditions (§5) reject any unsafe
   configuration with `ISOLATION REJECT` + exit 2 before anything runs.

2. **`tests/test_isolation_controls.sh`** (new) — 11 negative/positive
   controls proving the fail-closed behavior (§5), wired into
   `tests/run_pipeline.sh` as suite `test_isolation_controls` (one additive
   line; no existing suite was deleted, weakened, skipped, reclassified or
   reduced — the diff under `tests/**` is two new files + one added
   `run_suite` line).

3. **`Makefile`** — added `fuzz_decode`, `account_info`, `account_upgrade`,
   `rate_probe` to `BINS` (§6, defect found by the first fresh-root run).
   No recipe changed; `make all` now actually builds everything
   `check_registry --require-built` demands.

## 2. Isolated-root strategy

- **Root:** `mktemp -d /private/tmp/stp-p00-test-isolation.XXXXXX` — one
  fresh, empty, throwaway directory per run, under the release's
  `isolated_test_root_prefix`.
- **Repository copy:** `git clone --no-local` of the operational worktree at
  HEAD into `<root>/repo` (objects copied, never hardlinked; harness asserts
  clone HEAD == operational HEAD, both `0dbeb75` in the proof runs). The
  clone is a complete, self-sufficient checkout: vendored `third_party/`
  (OpenSSL, simdjson, ixwebsocket) and all test fixtures are git-tracked;
  no operational data enters the root.
- **State containment:** all suite writes are repo-root-relative
  (`work/test_results.ndjson`, `work/logs/**`, `build/**`, `.pytest_cache/`),
  so they land inside `<root>/repo/...`; `HOME` and `TMPDIR` point inside the
  root; post-run symlink scan proves no symlink resolves into the
  operational worktree (`escapes_to_operational=0` in both runs).
- **Environment scrub:** the four commands run under `env -i` with this
  exact allowlist (proof: `proof/child_env.txt` in each root):

      PATH=/usr/bin:/bin:/usr/sbin:/sbin
      HOME=<root>/home
      TMPDIR=<root>/tmp
      PYTHONUSERBASE=/Users/ritcardo/Library/Python/3.9   # user-site pytest/duckdb/pandas (packages only, no credentials)
      LANG=C  LC_ALL=C
      MAKEFLAGS=-j10
      PIPELINE_PORT_BASE=18400 (run 1) / 18500 (run 2)

  Everything else is denied by construction. No `~/.kalshi/env.sh` is ever
  sourced; no KALSHI/AWS credential exists in the child environment; no
  production API base URL is set — the code's fail-closed defaults
  (`KALSHI_ENV=local_mock`, `KALSHI_MODE=data_collect`, src/env.cpp) govern,
  and all networked suites talk only to 127.0.0.1 mocks started by
  `tests/run_pipeline.sh` itself.

## 3. Snapshot method (before AND after each run, per requirement 6)

Taken by the harness from the operational worktree
(`/Users/ritcardo/HFT BOT`), written only into `<root>/proof/`:

- complete `work/**` path+content manifest: SHA-256 + path of every file
  (1370 files, 139G), sorted;
- `.pytest_cache` state: per-file SHA-256 manifest;
- `git --no-optional-locks status --porcelain -uall` (all untracked paths)
  + HEAD hash;
- explicit hashes of the specifically forbidden operational files (§4);
- inherited environment recorded with secrets redacted
  (`proof/inherited_env_redacted.txt`: names always; values only for
  PATH/PWD/SHELL/TERM/LANG/USER/LOGNAME/HOME/TMPDIR/SHLVL/OLDPWD/_).

Verdict logic: any before/after diff, any test failure, or any symlink
escaping into the operational tree ⇒ non-PASS exit.

## 4. Results — two fresh isolated roots (requirement 7 & 8)

| | Run 1 | Run 2 |
|---|---|---|
| root | `/private/tmp/stp-p00-test-isolation.lo6cHY` | `/private/tmp/stp-p00-test-isolation.jUIDrq` |
| completed (UTC) | 2026-07-11 17:53:59 | 2026-07-11 17:58:16 |
| HEAD cloned | `0dbeb75` | `0dbeb75` |
| port base | 18400 | 18500 |
| `make all` | rc=0, 17s | rc=0, 18s |
| `make check` | rc=0, 7s (23 suites, ALL PASS) | rc=0, 7s (23 suites, ALL PASS) |
| `tests/run_pipeline.sh` | rc=0, 46s — **66 suites: 66 pass / 0 fail; 469 assertions pass / 0 fail** | rc=0, 47s — **66 suites: 66 pass / 0 fail; 469 assertions pass / 0 fail** |
| `check_registry --require-built` | rc=0 — `registry ok: 144 tools, 48 build targets covered` | rc=0 — same line |
| work/** manifest (1370 files) before vs after | **identical** (sha256 `82348e5fbff8d2879eb7c04ba9de51ed360bedf663bcdbc59e0ad9fadd519285`) | **identical** (sha256 `84fdee6f9e324d43a10885b06be9c958f7e0d8c0f784487d6c25e49249423ae0`) |
| `.pytest_cache` before vs after | identical (`38d9131174dc813cd984ca5945a3a1e10308a831ff96bf6f84db047776903aa2`) | identical (same sha) |
| git status (-uall) before vs after | identical (`054d50b14ec5f6a83f3153a95153f4420de439359fe9c3e8bc2a41041f1abad5`) — only pre-existing untracked `outputs/` + HEAD `0dbeb75` | identical (same sha) |
| forbidden-file hashes before vs after | identical (`46d51d67ab6e91b7b48b3b8b814b3e80bd182fd87b5b074e486412ac1bbde60b`) | identical (same sha) |
| symlink escapes into operational tree | 0 | 0 |

No suite was skipped: `tests/run_pipeline.sh` runs its fixed list
unconditionally and recorded 66/66 pass (the file `work/test_results.ndjson`
**inside each isolated root**); `make check` ran its full 23-suite list
(gates + pure + offline + 2 python warehouse suites, counted from
`proof/step2_make_check.log`). Nothing was staged or committed during the
runs (git status before==after, HEAD unchanged).

Forbidden operational files — hashes at both snapshots of both runs
(provenance: `proof/ops_forbidden_{before,after}.txt`, identical all four
times):

    b1881e0a03ad5a7083d1b1739cd3cb1ae0f581e16135c7700d7917c84272d3ba  work/lifecycle_status.json
    930b28b8d9aadeaf806a642f0de94109d8be8faae8fb95db450ccc126d014c44  work/lifecycle_events.ndjson
    b0bae29497e475c57ff7f759d9c3e1453659c4c47b07f91eff39af848764a1e4  work/test_results_latest.json
    6ab6cf5ad22111d138e3ad9417d239e05d8ee53ea5afccf257dec86ae835d67a  work/test_results.ndjson
    754d07ebf7533f418767f0ebfda9ce693d472ff37b0580e6c9838a8af31b6d35  work/live/alerts.log

**Between-runs observation (honest disclosure, D2):** run 1's and run 2's
work/** manifest hashes differ from each other (82348e5f… vs 84fdee6f…)
because exactly two files changed in the ~4-minute gap between runs:
`work/latency_baseline/sampler.out.log` and
`work/latency_baseline/samples.ndjson`. The writer is the pre-existing
production launchd job `com.ritcardo.rtt-baseline` (W-TL1 RTT sampler,
`tools/rtt_baseline_sampler.sh`), which appends one line every ~5 minutes —
completely independent of tests. Within each run's before/after window the
tree was byte-identical, so the isolation claim stands; this cross-run diff
additionally proves the change-detector actually detects changes (it is not
vacuously green). See residual limitation R2.

## 5. Fail-closed negative controls (requirement 9)

`tests/test_isolation_controls.sh` — 11 cases, each invoking the harness
preflight under a sanitized `env -i` base with exactly one injected
violation. Executed **three times, all 11/11 PASS**: standalone at commit
`0dbeb75` (log `/private/tmp/stp-p00-test-isolation-controls-standalone.log`,
sha256 `61c558a4714c8196125811200f7ab0fd7f784782edee36391edeb0a98608d174`),
and inside BOTH isolated runs as pipeline suite `test_isolation_controls`
(logs `<root>/repo/work/logs/test_isolation_controls.txt`).

| release requirement | control case | expected | result |
|---|---|---|---|
| missing isolation root | `missing-root-empty-arg` | reject (rc=2) | PASS |
| missing isolation root | `missing-root-nonexistent-path` | reject | PASS |
| root resolving to operational worktree | `root-is-operational-worktree` | reject | PASS |
| root resolving to operational worktree (symlink under the authorized prefix) | `root-symlinks-to-operational-worktree` | reject | PASS |
| inherited production credentials | `inherited-kalshi-credential` (KALSHI_API_KEY_ID) | reject | PASS |
| inherited production credentials | `inherited-aws-credential` (AWS_SECRET_ACCESS_KEY) | reject | PASS |
| production API URL | `inherited-production-api-url` (api.elections.kalshi.com in any var) | reject | PASS |
| production work/** path | `production-work-path-in-env` (`<ops>/work/test_results.ndjson` in any var) | reject | PASS |
| (guard) root outside authorized prefix | `root-outside-authorized-prefix` | reject | PASS |
| (guard) stale root | `non-empty-root-rejected` | reject | PASS |
| (positive sanity) valid fresh empty root | `valid-fresh-root-accepted` | accept (rc=0) | PASS |

Rejections report variable NAMES only — values are never printed (secrets
cannot leak into logs). The credential scan also rejects any variable name
matching SECRET/TOKEN/PASSWORD/PRIVATE_KEY/ACCESS_KEY/CREDENTIAL, any value
containing `kalshi.com`/`amazonaws.com`/the EC2 EIP `3.130.232.109`, and any
value referencing the operational `work/**` tree.

## 6. Defect found and fixed (why the Makefile changed)

The FIRST fresh-root run (preliminary, at pre-fix commit; root since
deleted, console log preserved at `/private/tmp/stp-p00-iso-run1-console.log`)
failed exactly one suite: `check_registry --require-built` inside
`run_pipeline.sh`, with

    REGISTRY FAIL
      - fuzz_decode: binary build/fuzz_decode not built
      - account_info: binary build/account_info not built
      - rate_probe: binary build/rate_probe not built
      - account_upgrade: binary build/account_upgrade not built

Root cause: these four are registered in `tools.json` without
`build: on_demand`, but `make all` did not build them — the operational
tree passed only via accumulated `build/` state from past ad-hoc builds.
This is precisely the hidden-state class this release exists to flush out.
Fix: add the four targets to `BINS` (Makefile). All other 63 suites passed
even in that preliminary run. No test or registry entry was changed.

## 7. Network posture (requirement 2 & 3)

- External networking: forbidden and not configured. The child environment
  contains no credentials, no API base URL, no SSH agent socket, no AWS/EC2
  variables; `~/.kalshi/env.sh` untouched (HOME points inside the root).
- Loopback mocks: `tests/run_pipeline.sh` starts its own 127.0.0.1 mock
  servers (mini_redis, mock_server, WS mocks) on PIPELINE_PORT_BASE
  18400/18500 — the only sockets the suites use, per the suites' design
  (offline-gated, `local_mock`/`data_collect` fail-closed defaults).
- Enforcement is by construction (env scrub + fail-closed defaults +
  loopback-only mock config), not by packet capture — see residual
  limitation R1.

## 8. Residual limitations

- **R1 — no packet-level proof of network silence.** The no-external-network
  claim rests on the scrubbed environment, the code's fail-closed safe-mode
  defaults, and the suites' loopback-only mock design — not on a network
  sniffer. An auditor wanting stronger evidence can rerun the harness under
  a packet capture.
- **R2 — a live production writer touches operational `work/**`.** launchd
  job `com.ritcardo.rtt-baseline` appends to `work/latency_baseline/*` every
  ~5 minutes. Both proof runs had clean windows, but a future isolated run
  can show a spurious `FAIL_STATE_CHANGED` whose diff is confined to
  `work/latency_baseline/*` with the sampler as the cause. Deliberately NOT
  masked in the harness (an exclusion list could hide real violations);
  auditors should inspect any diff and attribute it.
- **R3 — proof files live under `/private/tmp`** (roots
  `…lo6cHY`, `…jUIDrq`, the standalone-controls log, the preliminary-run
  console log) and vanish on reboot. All decision-relevant hashes/counts are
  recorded here and in the manifest; everything is regenerable by rerunning
  `tests/isolated_run.sh --new`.
- **R4 — no `tools.json` entries for the two new test scripts.** `tools.json`
  is outside this release's allowed writes. `check_registry` stays green (it
  scans `tools/*` and `work/research/*`, not `tests/**`), and the scripts are
  reachable via the registered `run_pipeline` entry; if the operator wants
  first-class registry entries, the smallest amendment is: allow one
  `tools.json` edit adding `isolated_run` + `test_isolation_controls`
  entries (kind=check/test, safety=offline).
- **R5 — PYTHONUSERBASE exception.** Full HOME isolation has one documented
  carve-out: `PYTHONUSERBASE=/Users/ritcardo/Library/Python/3.9` so the
  user-site pytest/duckdb/pandas installs resolve. This grants package
  imports only; no credential lives there.

## 9. How to reproduce (for the independent auditor)

    cd "/Users/ritcardo/HFT BOT"        # note the space in the path
    bash tests/isolated_run.sh --new    # third fresh root; ~10 min (139G hashed twice)
    bash tests/test_isolation_controls.sh

Expected: `isolated_run VERDICT: PASS`, 66/66 pipeline suites,
`registry ok: 144 tools`, all snapshot pairs identical (modulo R2),
controls 11/11.
