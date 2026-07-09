# W-A2 — Linux validation log (2026-07-09)

**Verdict: ✅ PASS — the whole pipeline is proven correct on the EC2 box.**
Box: i-0fd427becf740a06b (r8g.large, 2 vCPU/16 GB, Ubuntu 24.04.4 arm64,
us-east-2, 13.59.9.97). Repo HEAD at final run: 2f0c1b3. Capture NOT started
(W-A4 owns that); Mac remained the sole capture owner throughout (P4).

## Acceptance evidence (in W-A2 definition order)

1. **`make check` GREEN on the box** — exit 0, all ALL PASS
   (final run 2026-07-09T03:56Z; evidence `~/wA2_validation.log` on the box).
2. **`tests/run_pipeline.sh` fully GREEN on the box** — `== PIPELINE PASS ==`,
   exit 0, **50/50 suites pass, zero fail records** in work/test_results.ndjson.
   Mocks only; no live orders anywhere (the battery is offline by design).
3. **`check_registry` green** — runs inside run_pipeline with
   `--require-built`; passes after the fix noted below.
4. **ONE read-only preflight from the EC2 IP** (operator-run, S4 — creds never
   touched by the agent):
   - REST leg: `preflight --prod-ok` (no --order) — exit 0 proven by the `&&`
     chain (the WS leg cannot start otherwise; the earlier mis-scoped attempt
     that fail-closed to local_mock and short-circuited demonstrates exactly
     this gate). Targets `https://external-api.kalshi.com` (us-east-2-native
     host, no BASE_URL override needed).
   - WS leg: **10 s read-only `ws_shadow`** — `WS SHADOW PASS`, env=prod
     mode=data_collect read_only orders_blocked; **5,815 events (578 trade /
     5,237 tick), reconnects=0, errors=0, overflow=0, recorder drop=0,
     exec transmitted=0, write-token spend=0**; 5,817 lines captured to
     `work/probe/wA2_ws_preflight.ndjson` (isolated path — never work/raw,
     depth_probe discipline; not tailed by any ingester).

## Declared deviation (for the independent audit)

The plan text says "a 10 s `ws_smoke`". `ws_smoke` is **by design a
mock-integration smoke** (hardcoded `api_key_id="smoke"`, fake `SMOKESIG`
signer, subscribes mock market MKT-A — apps/ws_smoke.cpp): pointed at prod it
can only 401. The plan's *intent* — prove the exchange is reachable and auth
valid over WS from the EC2 IP — was satisfied with a 10 s read-only
`ws_shadow` run instead (the same harness the production supervisor runs,
which also makes it the more faithful preflight). `ws_smoke` itself passed
against the mock inside run_pipeline (suite `ws_smoke`, 1268 ms).

## Portability fixes shipped (W-A2 allowed writes: test-only Linux fixes)

| Fix | Root cause | Proof |
|---|---|---|
| venv gains `pyyaml` (bringup_ec2.sh) | `import yaml` is LAZY (inside function bodies: mm_research.load_fee_facts, build_classification) — invisible to top-level import scans; test_research_metrics + test_gate_metrics failed with ModuleNotFoundError | both suites pass in final battery; lazy-import sweep found `yaml` as the only missing third-party module |
| build the 4 registry binaries (fuzz_decode, account_info, rate_probe, account_upgrade) | not in the default make target; `check_registry --require-built` fails on a fresh box | check_registry passes in final battery |
| Makefile: `-fsanitize-ignorelist=` made clang-conditional (`FUZZ_IGNORELIST`) | flag is clang-only; g++ (the Linux default via make's built-in CXX) rejects it | **`make fuzz` 200k iters: no crash/UB on BOTH platforms** — box/g++ (simdjson inlines instrumented, still clean) and Mac/clang (ignorelist active). Re-prove after any simdjson upgrade (noted in Makefile) |

**Forbidden-writes compliance:** zero changes to capture/ingest/export
behavior. The Makefile change touches only the fuzz_decode (test binary) rule;
bringup_ec2.sh is deploy tooling. `make check` re-verified green on the Mac
after the Makefile change (fuzz rebuild + run included).

## Environment facts recorded

- python 3.12.3, venv: duckdb==1.4.5 (pinned to Mac), numpy, pandas, pytest,
  pyyaml 6.0.3.
- Production binaries: g++ 13.3.0. fuzz_decode on Linux: g++ with ASan+UBSan,
  no ignorelist (clean by measurement). clang-18 present but unused for
  std::expected reasons (clang-18 + libstdc++ lacks it).
- On-box evidence file: `~/wA2_validation.log` (final battery tee),
  `work/probe/wA2_ws_preflight.ndjson` (+ .metrics) — probe artifacts, safe
  to delete any time, never read by ingest.

## Independent audit outcome (2026-07-09)

**✅ PASS — 0 blocking, 4 non-blocking (all evidence-hygiene).** The auditor
re-measured everything measurable (box state over read-only SSH, both diffs,
Mac battery + fuzz re-run, probe-file sampling, credential grep) and
confirmed: scope test-only; 50/50 suites pass on box; capture NOT started;
work/raw absent on box; transmitted=0 structurally implied by WS SHADOW PASS
(ws_shadow.cpp:610); no secrets anywhere. Non-blocking, applied/queued:
1. Operator-run preflights now **tee to a durable log on the box**
   (`~/wA<N>_preflight.log`) — REST-leg stdout of this session lives only in
   the operator terminal (mechanism honestly stated; WS leg has artifacts).
   BINDING for W-A3/W-A4 operator commands.
2. Same tee rule covers the WS-leg stdout in future runs.
3. The EC2→Kalshi RTT line in W-A0 RESULT stays [ESTIMATE] — **measure during
   the W-A4 preflight** (or W-LAT-BENCH) and update it.
4. Makefile FUZZ_IGNORELIST made lazy (`=`) so the compiler probe doesn't run
   on every make parse; on-box fuzz count evidence is artifact-inferred
   (binary + scratch mtimes + hardcoded 200000) — future fuzz runs tee an
   `FUZZ_RC=` line into the validation log.

## What W-A2 did NOT do (by design)

No `ws_shadow` firehose service start, no systemd enable, no S3/IAM (W-A3),
no catalog fetch (single REST owner stays the Mac until W-A4 step 4). The
10 s WS run was a second concurrent connection — exempt per the 2026-07-06
three-connection test — and its capture went to the isolated probe path.
