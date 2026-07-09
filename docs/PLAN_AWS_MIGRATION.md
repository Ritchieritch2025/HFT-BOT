# PLAN — AWS / EC2 MIGRATION (MASTER_SEQUENCE STEP 1)

**Phase:** infra for Phase 1→2 (24/7 always-on capture). **Gate satisfied:** none
directly — this is the substrate every later gate depends on. A laptop that sleeps
can never produce a clean day (07-08 was **36.7% down** to Mac Deep-Idle sleep;
`docs/plan_audits/capture_gap_taxonomy_2026-07-08.md`). Interim mitigation (`pmset
-a disablesleep 1`) is live; this migration is the permanent fix.

**Status: FULL DRAFT.** Drafting needs no AWS account. **EXECUTION (W-A1 onward) is
gated on the operator** having an AWS account and creating all
credentials/`~/.kalshi/env.sh` by hand on the box (**S4 — the agent never touches
account creation or credentials**). W-A4 cutover is **operator go/no-go, operator
present.** One W per fresh session, independent audit after each (as the capture Ws).

## Governing rules (bind every W-A)
- **S4:** account + credentials are operator-only; `env.sh` is hand-created on the
  box (600), never via repo or agent, never printed.
- **P4:** the Mac pipeline is live revenue-critical until W-A4 completes. Nothing
  breaks Mac capture until EC2 is *proven*; the cutover keeps WS on **both** boxes
  so capture never drops. Mac launchd stays **installed but dormant** through
  cutover = instant rollback.
- **Single REST owner** (W-A4): never two REST pollers at once (rate-budget +
  catalog-writer race). WS concurrency is exempt (2026-07-06 three-connection test).
- **E1/D4:** every code/config change ships a test; `make check` +
  `tests/run_pipeline.sh` green **on the EC2 box** (W-A2) before cutover.
- **D1:** raw/archive are the source of truth; S3 copies are versioned, restore is
  proven BEFORE cutover (W-A3), never destroy Mac data until EC2 is authoritative.
- **Credential model (operator ruling 2026-07-08): long-lived IAM-user access keys
  in `~/.kalshi/env.sh` on the EC2 box** (NOT an instance role). Operator
  hand-creates env.sh (600, S4); the agent never reads/prints/places it. Because the
  key is a standing secret on a 24/7 host, the mitigations below are load-bearing,
  not optional.
- **Least-privilege IAM (makes D1 structural, not just procedural):** the IAM **user
  whose keys go in env.sh** gets `PutObject`/`GetObject`/`ListBucket` on the vault
  prefixes but **NO `DeleteObject` (and no lifecycle-expiry) on
  raw/archive/catalog prefixes** — so a bug or a leaked key *cannot* erase the
  vaulted history. Back it with S3 Object Versioning + a bucket policy denying delete
  on those prefixes. Deletes, if ever needed, are a separate operator action with
  separate credentials. Standing-secret hygiene: env.sh 600; the key is never in the
  repo/logs/reports; plan a rotation (W-A5 records the cadence).

---

## W-A0 — sizing gate (paper + read-only probe)
Purpose:        Confirm the instance is big enough BEFORE any bring-up; decide
                where the memory-heavy gold builds run.
Blocked by:     operator has launched an EC2 instance (else W-A0 is paper: state
                the required floor).
Allowed reads:  the instance `nproc` / `free -g` / `lsblk` (read-only over SSH);
                docs/warehouse_schema.md (peak-size facts).
Allowed writes: docs/PLAN_AWS_MIGRATION.md (record the measured sizing + decision).
Forbidden writes: anything on the box beyond reading; no installs.
Acceptance:     printed vCPU / RAM / EBS, **sized for the POST-migration load, not
                today's** — rider (b) promotes ALL categories to class_a_full_l1, so
                the box must carry the full-market L1 subscription universe
                (subscription count, bandwidth, storage/day), materially more than
                the current filtered set. Recommend ≥ 8 vCPU / **32 GB** / 200 GB
                gp3; the WS capture is light, the warehouse build is the RAM driver.
                **≥32 GB is the strong path** — gold builds run ON EC2 and the Mac
                becomes truly dormant (W-A5). If RAM < 32 GB is accepted, the daily
                gold build **stays on the Mac reading from S3** (17.7 GB peak) — a
                deliberate split; in that branch the Mac is **NOT dormant** (it runs
                a wake-scheduled daily build) and W-A5 does **NOT** fully re-enable
                Mac sleep (see W-A5). Pick the branch here so W-A5 is unambiguous.
Rollback:       n/a (read-only).
Exit evidence:  the sizing line + the gold-build-location decision in this doc.

### W-A0 RESULT (2026-07-08, paper branch — no instance exists yet)

**BRANCH DECISION (operator, 2026-07-08): ≥32 GB — gold builds run ON EC2; the
Mac retires FROM THE PRODUCTION PIPELINE after W-A5** — no scheduled wakes, no
builds, sleep fully re-enabled. "退役" scope, precisely: the Mac still (a) runs
the small daily report-landing launchd job (W-A5 report flow-back — works fine
on a sleeping Mac, runs when awake, reports arrive late never lost), and
(b) keeps its dormant launchd + S3 restore capability as the rollback host —
so the Mac must NOT be wiped. Consequence of the branch: the W-A5 "<32 GB
exception" paragraph is **n/a**.

**Sizing mandate (operator, this session):** size for the POST-expansion load —
rider (b)/playbook 3e full-market L1 + 3g sports-first depth watchlist + the
future STEP 4 full depth rollout — NOT today's volume.

#### Design-load ledger (every number carries provenance)

| Input | Value | Source |
|---|---|---|
| Firehose raw capture | 21–22 GB/day measured on gap days (07-07/07-08 `du work/raw/date=*`); ~24–30 GB/day at true 24/7 uptime [ESTIMATE, uptime-corrected — re-baseline after the first clean EC2 day; audit note: 07-08 hit 23 GB while still accruing, so the top of this bracket may be low] | this session, measured |
| Rider (b)/3e full-market L1 | **raw unchanged** — capture already records all categories; only ingest discards Class B today. Ingest/archive/gold input rows grow ~1.3–2× [ESTIMATE] | DATA_COMPLETENESS_ROADMAP 拼图① |
| 3g depth watchlist N=50 | +2.4 GB/day (30× expected) … +7.9 GB/day (100× conservative); ≤565 msg/s peak | PLAN_DEPTH_EXPANSION §3 [PROBE-PENDING brackets] |
| STEP 4 full depth N=500 | +15.7 … +52.5 GB/day; ~3,000 msg/s conservative peak | PLAN_DEPTH_EXPANSION §3 [PROBE-PENDING brackets] |
| Design raw/day (worst case) | 30 + 52.5 = 82.5 ⇒ **~85 GB/day** (rounded UP) ⇒ 3-day raw window ≈ **250 GB** | derived from rows above |
| Gold build memory | ru_maxrss 6.33 GB, peak footprint 17.7 GB incl. compressor, at today's 8.2 M-record day | SESSION_LOG 2026-07-07 (measured) |
| Mac reference frame | the Mac has **16 GB RAM** (`hw.memsize`, measured this session), arm64, 10 cores — the 17.7 GB peak already exceeds physical RAM (macOS compressed memory/swap absorbs it) | this session, measured |
| Gold build at STEP 4 scale | input rows grow ~5–12×; memory scaling law UNKNOWN (streaming vs linear) — this is the argument for 64 GB, not 32 | honest unknown; DuckDB spill is the fallback |
| CPU | decode+apply 281.9 ns/msg ⇒ capture is ~0.1% of one core even at 4 k msg/s; vCPUs are for the daily zstd-15 build, not capture | kalshi_facts.latency [VERIFIED-MEASURED] |
| Bandwidth | firehose 472 ev/s + depth ~3 k msg/s × 270 B wire ≈ ~1 MB/s ≈ 8 Mbit/s — trivial vs any instance's network | kalshi_facts + PLAN_DEPTH_EXPANSION §2.3 |

#### Region — **us-east-2 (Ohio)**, decided

`dig external-api.kalshi.com` / `external-api-ws.kalshi.com` (2026-07-08) both
CNAME to `elections-external-api-107430227.us-east-2.elb.amazonaws.com` —
Kalshi's external Trade API terminates in AWS us-east-2. Same-region EC2 gives
the lowest latency path and free same-region S3 transfer. (Kalshi also offers
AWS **PrivateLink** for these hosts — institutional@kalshi.com — a future
latency/isolation option, not needed for capture. From EC2 prefer the
`external-api*` hosts over `api.elections.kalshi.com`, which resolves to
CloudFront; host switch is a W-A2 checklist item, signature payload unchanged.)

#### Instance choice — **r8g.2xlarge** (8 vCPU / 64 GB, Graviton4), on-demand

Prices: ec2.shop API, us-east-2, on-demand Linux, fetched 2026-07-08 (verify
the console quote at launch; reserved = 1-yr no-upfront):

| Instance | vCPU/RAM | $/mo on-demand | $/mo 1-yr reserved | Verdict |
|---|---|---|---|---|
| **r8g.2xlarge** (recommended) | 8 / 64 GB | **$344** | $228 | covers STEP 4 full depth without betting on the unknown gold-build memory scaling; no resize (= no capture gap) ever needed |
| m8g.2xlarge (budget) | 8 / 32 GB | $262 | $173 | fine through 3g N=50; at STEP 4 the RAM is a measured gamble — resize to 2xlarge later costs a stop/start capture gap |
| r8g.xlarge (floor) | 4 / 32 GB | $172 | $114 | meets the ≥32 GB branch letter but not the sizing mandate (build hours on 4 cores at 10× data; thin RAM) |
| r7i.2xlarge (x86 fallback) | 8 / 64 GB | $386 | $256 | only if ARM-Linux surprises appear in W-A1/W-A2 (terminate + relaunch is cheap pre-cutover; remember to first disable termination protection AND delete the orphaned 300 GB volume — delete-on-termination=No means it silently keeps billing $24/mo) |

Why Graviton (aarch64): the dev Mac is arm64 Apple Silicon — the whole codebase
(incl. simdjson NEON, E4 integer fixed-point) already builds and passes tests
on ARM64; Graviton4 is ~11% cheaper than the x86 equivalent. W-A2's full test
battery ON the box is the proof gate either way. Endianness identical.

Why not spot: 24/7 revenue-critical capture; interruption = capture gap by
design. On-demand until the cutover is proven; the reserved/savings-plan
purchase is an operator decision deferred to W-A5's cost section.

**Latency scope note (operator Q&A 2026-07-08): this box is the DATA
pipeline host, not the future trading host — its ISA is NOT a latency
decision.** Measured ladder: Mac→Kalshi RTT ~30 ms (curl, 2026-07-08);
same-region EC2 ~0.2–1 ms [ESTIMATE, measure in W-A2 preflight]; our decode
281.9 ns/msg — so the region move captures >99% of the latency win and
ARM-vs-x86 differs at the sub-microsecond level, noise on this path. The
**trading box is a SEPARATE Phase 4/STEP 6 decision** with its own levers,
none ISA-bound today: dedicated small high-clock instance (x86 c-family a
live option there), same-AZ placement, **Kalshi PrivateLink**
(institutional@kalshi.com; REST+WS interface endpoints, traffic stays on the
AWS backbone), persistent connections + hot-path engineering (E7). Decide it
then, with measured numbers, on a box that costs tens of $/mo to add.

#### EBS — **300 GB gp3 at launch, grow online before STEP 4**

- Launch: **300 GB gp3** (defaults: 3,000 IOPS / 125 MB/s — ample; avg write
  <1 MB/s). Cost **$24/mo** ($0.08/GB-mo us-east-2, AWS published rate —
  verify on the console quote).
- Working set today+3g: raw window 79 GB (expected bracket) to ~114 GB (3g
  conservative 100×) + staging ~5–10 GB + local archive + repo/build/OS ~20 GB
  + rotated metrics 1.5 GB (rider (a)) ⇒ ~110–150 GB, so 300 GB is ≥2×
  headroom even on the conservative bracket.
- Before STEP 4 N≥200: grow the volume online to **500 GB** (+$16/mo; gp3
  grows with NO downtime, no capture gap; can't shrink — that's why we don't
  start at 500). Local archive retention policy (S3 is the vault) is W-A5's
  cost-budget item.

#### Monthly budget line (black and white)

| Item | $/mo |
|---|---|
| r8g.2xlarge on-demand | 344 |
| EBS 300 GB gp3 | 24 |
| Public IPv4 (Elastic IP — since 2024-02 AWS bills ~$0.005/hr for EVERY public IPv4, attached or not) | ~4 |
| S3 (initial vault ~54 GB raw + archive; ongoing sync) | ~3–5 |
| Egress (reports S3→Mac; ingress is free) | <1 |
| **Total at launch** | **~$380/mo** (→ ~$260/mo if/when 1-yr reserved, W-A5 decision) |
| STEP 4 delta (EBS 500 GB, S3 growth) | +$20–30/mo |

#### W-A0 REVISION (operator budget constraint, 2026-07-08 — supersedes the
#### instance/EBS choice above; the region, branch, and latency notes stand)

**Operator ruling:** budget is NOT hundreds of $/mo pre-profit; EVERYTHING runs
on ONE machine (capture + warehouse + detectors + the future trading process);
STEP 4 full-depth expansion is DEFERRED until the system is profitable — so
W-A0 no longer sizes for the N=500 worst case. The r8g.2xlarge/$380 pick is
WITHDRAWN (it priced the deferred ceiling).

Revised ladder (ec2.shop us-east-2, fetched 2026-07-08; totals incl. EBS +
public-IPv4 + S3):

| Option | Config | Total $/mo | Notes |
|---|---|---|---|
| 0 — defer migration | Mac stays (sleep-disable live) | 0 | home ISP/power/operator-use risk remains; clean-days accumulate at the mercy of the laptop |
| **1 — bootstrap (recommended)** | **r8g.large 2 vCPU/16 GB** + 200 GB gp3 | **~$110** ($86+$16+$4+~$3) | gold build measured ru_maxrss 6.33 GB runs on the 16 GB Mac daily — same RAM, now with guardrails (below); nightly build slower on 2 cores (batch, harmless); 3g N=50 fits (+2.4–7.9 GB/day inside 200 GB) |
| 2 — comfort | r8g.xlarge 4 vCPU/32 GB + 300 GB gp3 | ~$195 | no RAM care needed |

**16 GB guardrails (W-A1 items, free):** systemd `MemoryMax` on the gold-build
unit + DuckDB `memory_limit` (build degrades to spill, never evicts others);
swap file as backstop; ws_shadow unit gets `OOMScoreAdjust=-1000` — worst case
is ALWAYS "report is late", never "capture died" (S2 fail-closed). Upgrade
path when profitable: instance resize = stop/start ~3 min (schedule off-peak,
gap noted honestly) + EBS grows online; no rebuild.

**Branch decision restated for one-box reality:** gold builds run ON EC2 even
at 16 GB (the ≥32 GB/"<32 GB ⇒ gold stays on Mac" dichotomy above is
superseded — the Mac retires from the pipeline either way; the 32 GB line was
comfort, not feasibility, per the measured 6.33 GB RSS). Phase 4 note: the
trading process shares this box — `nice` the nightly build below it; revisit
sizing with profit, not before.

**Free-credit note [VERIFY AT SIGNUP]:** post-2025-07 new AWS accounts get
~$100 signup credit (+ activity credits, up to $200 total) and a 6-month free
plan — month 1–2 may be near-free.

**OPERATOR PICKED OPTION 1 (2026-07-08)** — instance launched:
`i-0fd427becf740a06b`, r8g.large, Ubuntu 24.04 arm64, us-east-2, 200 GB gp3,
SSH restricted to operator IP, key `YINQIAN` (operator-held).

**W-A0 CLOSED (2026-07-08, measured on the box over SSH, read-only):**
`nproc`=2 · `free -g`=15 (16 GiB nominal) · `lsblk` nvme0n1=200 GB (root
199 GB) · aarch64 · Ubuntu 24.04.4 LTS — matches option 1 exactly.
Acceptance met: vCPU/RAM/EBS printed, post-migration-load sizing recorded
above, gold-build location = EC2 (branch decision). Public IP at W-A1 time:
13.59.9.97 (Elastic IP association still recommended — checklist step 11).

#### Operator boot checklist（照着点；从"Launch instance"起开始计费——
每小时费率见上表所选档位）

1. 登录 AWS 控制台，右上角 region 切到 **us-east-2（Ohio / 俄亥俄）**。
2. EC2 → **Launch instance**（启动实例）。
3. Name（名称）: `kalshi-pipeline-1`。
4. AMI（系统镜像）: **Ubuntu Server 24.04 LTS**，Architecture 选 **64-bit (Arm)**。
5. Instance type（机型）: **按上表所选档位**（方案 1 = r8g.large，确认页显示 2 vCPU / 16 GiB；方案 2 = r8g.xlarge，4 vCPU / 32 GiB）。
6. Key pair（密钥对）: Create new key pair → 类型 **ED25519**，格式 .pem，
   下载后保存好（这是登录钥匙，丢了要换锁；不要发给任何人，包括我）。
7. Network settings（网络）: 默认 VPC 即可；**Create security group**，只勾
   **Allow SSH traffic from → My IP**（务必选 My IP，不要 Anywhere）。
   不勾 HTTP/HTTPS（本机不对外服务）。
8. Configure storage（磁盘）: 改成 **所选档位对应容量（方案 1 = 200 GiB，方案 2 = 300 GiB），gp3**（IOPS/吞吐留默认
   3000/125）。Advanced 里把这块盘的 **Delete on termination 改成 No**
   （实例误删时数据盘保留）。
9. Advanced details（高级）最下方: **Termination protection → Enable**
   （防误删）。其余全部默认；**不要**挂 IAM role（凭证走 env.sh，S4）。
10. 右侧 Summary 核对：r8g.2xlarge / 300 GiB gp3 / us-east-2 → **Launch
    instance**。此刻开始计费。
11. 实例页 → Elastic IPs → **Allocate** 一个并 **Associate** 到该实例
    （固定公网 IP。注意：2024 年起 AWS 对所有公网 IPv4 收 ~$3.65/月，
    挂不挂都收，已计入上面预算表；EIP 相对普通公网 IP 不多花钱，
    但换机型/重启时 IP 不变，省去改防火墙和脚本）。
12. 把「公网 IP + 你本机 `ssh -i 密钥.pem ubuntu@IP` 能登上」发回来。
    我下一步（W-A0 收尾）只做三条只读命令：`nproc` / `free -g` / `lsblk`，
    核对 8 / 62-64 / 300 后 W-A0 关闭，进入 W-A1。

**STOP — 花钱节点：以上每一步都由操作员执行；agent 不碰账号（S4）。批准即
照单点击；不批准则本清单作废重议，无任何已产生费用。**

## W-A1 — hardening & bring-up
Purpose:        Turn a bare Ubuntu box into a locked-down, reproducible pipeline
                host — repo, deps, time sync, service units — WITHOUT capture yet.
Blocked by:     W-A0.
Allowed reads:  the repo; deploy/ (created here); the box.
Allowed writes: `deploy/` — **systemd** unit files (pipeline supervisor, ingest)
                replacing the Mac's launchd; a bring-up script (clone, build-deps,
                modern Python ≥3.12 via pyenv/apt, chrony for time sync replacing
                macOS sntp, ixwebsocket/simdjson/openssl vendored build); an
                **SSH-only-from-operator-IP** security checklist (printed, applied
                by operator); README in deploy/.
Forbidden writes: `~/.kalshi/env.sh` (operator hand-creates it — S4); any secret;
                capture/ingest/export SOURCE (only build + service wiring).
Acceptance:     box builds `build/ws_shadow` + all binaries clean; systemd units
                load (dry, capture NOT started) and **reproduce the three
                load-bearing launchd behaviors** — (a) **top-of-hour respawn /
                hourly raw-log rotation** (the warehouse is built on hourly raw
                partitions; a wrong rotation cadence breaks the three-layer build),
                (b) the **single-instance lock** (systemd + app flock — never two
                ws_shadow writers), (c) the app's **internal W-C1 watchdog** must
                own reconnect, so systemd uses `Restart=on-failure` only (NOT
                `WatchdogSec=`) to avoid two supervisors fighting (restart storms /
                double reconnect). chrony synced (offset printed); SSH restricted to
                the operator IP; **egress allow-listed to 443 (Kalshi + AWS/S3
                endpoints)** — not left at the AWS all-allowed default; `env.sh`
                present (operator-made, 600) and the auth smoke (REST
                `/exchange/status`) passes.
Rollback:       terminate/rebuild the box; nothing on the Mac touched.
Exit evidence:  commit (deploy/ units + script); the hardening checklist output.

### W-A1 RESULT (2026-07-09 UTC — DONE, operator-判定收官)

Evidence (all measured on the box unless noted):
- **Code delivery:** Mac→box bare-repo push over SSH (`~/kalshi.git` →
  `~/hft-bot`), zero GitHub credentials on the box (S4). HEAD at close:
  d231452 + this session's exit commits.
- **Build:** all binaries clean on **g++ 13.3.0** (readelf-verified; GNU
  make's built-in CXX=g++ overrides the Makefile's `?=` intent — clang
  installed as fallback only, deliberately unused so validation matches the
  production binaries).
- **make check: exit 0 on the EC2 box AND on the Mac at the same commit.**
  One portability fix was needed: `include/kalshi/env.hpp` lacked
  `#include <cstdint>` (libc++ provides `std::uint8_t` transitively,
  libstdc++ does not). Zero behavior change. SCOPE NOTE: strictly a
  W-A2-class fix ("Linux portability"), executed here because the operator
  instructed `make check` on EC2 within the W-A1 session.
- **Time:** chrony → Amazon Time Sync (169.254.169.123, link-local, survives
  egress lockdown); offset **6.9 µs**. Timezone UTC.
- **One-box guardrails (16 GB):** 16 GB swapfile active (swappiness 10);
  `kalshi-oom-guard.timer` firing every 60 s (ws_shadow → oom_score −1000,
  batch python → +300 / nice+10 / ionice-7); pipeline unit OOMScoreAdjust −600.
- **systemd:** `kalshi-pipeline.service` **installed, disabled, inactive**
  (dry — W-A4 owns first start); Restart=on-failure only (no WatchdogSec);
  exit-78 (missing env.sh) stays stopped visibly. The three load-bearing
  launchd behaviors remain app-level (see deploy/README_EC2.md).
- **Credentials (S4, operator-executed):** `~/.kalshi/env.sh` (127 B) +
  `private_key.pem` (1,679 B), both 600, scp'd by the operator; agent's
  attempt to source env.sh was correctly blocked by the permission layer —
  the **auth smoke was run BY THE OPERATOR**: `preflight --prod-ok`
  (read-only, no --order) ⇒ **PREFLIGHT PASS**, order-execution line untested
  as expected (operator-reported 2026-07-09).
- Disk after bring-up: 20 G / 193 G used. SSH key relocated to
  `~/.ssh/kalshi-key.pem` (400) on the operator's Mac.

**Acceptance deviations (operator-ruled 2026-07-09, both due before W-A4):**
1. **Egress allow-list to 443 NOT yet applied** — deferred; steps in
   deploy/SECURITY_CHECKLIST_EC2.md §4–5.
2. **Elastic IP not associated (DONE 2026-07-09: 3.130.232.109)** — 13.59.9.97 is ephemeral (changes on
   stop/start; breaks the Mac's `ec2` git remote + RUNBOOK). Checklist item 11.
Both are W-A4 *prerequisites*: the cutover go/no-go checklist must verify
them first.

## W-A2 — Linux validation (prove it on the box)
Purpose:        Prove the whole pipeline is correct on Linux before trusting it
                with capture.
Blocked by:     W-A1.
Allowed reads:  the repo on the box.
Allowed writes: any test-only fixes for Linux portability (path/endianness/clock);
                a validation log.
Forbidden writes: behavior changes to capture/ingest/export.
Acceptance:     **`make check` + `tests/run_pipeline.sh` fully GREEN on the EC2
                box** (mocks, no live orders); `check_registry` green; ONE
                read-only preflight (`/exchange/status` + a 10 s `ws_smoke`) from
                the **EC2 IP** confirming the exchange is reachable + auth valid
                from AWS. No `ws_shadow` firehose yet (that's W-A4).
Rollback:       n/a (tests).
Exit evidence:  the green test tails from the box; the preflight output.

### W-A2 RESULT (2026-07-09 — ✅ PASS)

Full evidence + declared deviation (ws_smoke is mock-only by design ⇒ the WS
leg used a 10 s read-only ws_shadow) + the three test-only portability fixes
(pyyaml lazy import; 4 registry binaries built; fuzz ignorelist flag made
clang-conditional, g++ 200k-iter fuzz clean): see
**docs/plan_audits/wA2_linux_validation_2026-07-09.md**. Battery: make check
exit 0 + run_pipeline `PIPELINE PASS` (50/50 suites) + check_registry green,
all ON the box; REST+WS preflight from the EC2 IP operator-run (S4):
preflight exit 0, WS SHADOW PASS 5,815 events/10 s, transmitted=0. Capture
still NOT started (W-A4). NEXT: W-A3 needs the operator's S3 bucket + IAM
user (S4) per the credential-model rules above.

## W-A3 — S3 vault + RESTORE TEST (before cutover)
Purpose:        Get the Mac's accumulated data safely into S3, restore-verified,
                BEFORE the cutover — so a cutover failure can never lose history.
Blocked by:     W-A2; operator has created the S3 bucket + IAM (S4).
Allowed reads:  Mac `work/warehouse/archive`, `work/raw` (retention window),
                `work/warehouse/catalog`.
Allowed writes: a sync script → S3 (**versioned** bucket; `internal/` and
                `product/` prefixes per warehouse_schema); a **RESTORE TEST**
                script; provenance manifest.
Forbidden writes: DELETE anything on the Mac (D1 — Mac stays authoritative until
                W-A4); never `--delete` on the sync.
Acceptance:     archive + raw(3 d) + catalog uploaded (byte/md5 verified against
                manifest.csv); **RESTORE TEST MANDATORY** — pull a sample partition
                back from S3 to a scratch dir and diff it byte-for-byte against the
                Mac original (must be identical). Cost of the vault estimated.
Rollback:       delete the S3 objects (versioned, reversible); Mac untouched.
Exit evidence:  upload manifest + the restore-diff = 0 result.

### W-A3 RESULT (2026-07-09 — ✅ PASS)

Vault live: `s3://kalshi-vault-ritcardo/mac-vault/` — **1,082 objects /
85.52 GB** (raw 4 days incl. at-risk 07-06 synced first; warehouse
facts/dim/catalog/legacy_greed/_meta + manifest.csv; the 6 MD5 account books
under meta/). **1,076/1,076 data objects MD5-verified end-to-end** (Mac md5 →
box md5 → S3 ETag, single-part uploads so ETag==MD5). **RESTORE TEST passed
on two legs** (largest raw file 663 MB + an archive partition), byte-for-byte
`cmp` on the Mac AND md5 == manifest.csv's file_md5. Cost ~$2.0/mo measured.
Nothing deleted anywhere (D1); no `--delete` anywhere. One torn mid-write
copy (hour-16 retry segment) was CAUGHT by the hop-1 md5 check and fixed
after the hour closed — W-A5's delta sync must exclude
`firehose_<current-hour>.ndjson*` (glob). Full evidence + no-fabrication
provenance table + declared deviations:
**docs/plan_audits/wA3_s3_vault_2026-07-09.md**; on-box `~/wA3_vault.log`.
Box staging copy (~/vault_staging, 79 GB) kept until W-A4 completes.
NEXT: W-A4 (zero-gap cutover, operator go/no-go + present) — its
PREREQUISITES block below: EIP ✅ DONE (3.130.232.109, 2026-07-09);
egress-443 still pending; work/live exists ✅ (W-A1 fix).

## W-A4 — zero-gap cutover (operator go/no-go, operator present)
Purpose:        Move live capture Mac→EC2 with ZERO gap and a clean REST handoff.
Blocked by:     W-A3 green + operator go/no-go.

**PREREQUISITES (from W-A1 deviations + audit, verify FIRST in the go/no-go):**
1. Elastic IP associated (the W-A1-era 13.59.9.97 is ephemeral; a stop/start
   before cutover would silently break the Mac's `ec2` git remote + RUNBOOK).
2. Egress locked to 443 per deploy/SECURITY_CHECKLIST_EC2.md §4, verified §5.
2b. **Delete-denial TEST (from W-A3 audit — design is untested):**
   operator-witnessed `aws s3 rm` attempt on a scratch key under
   `mac-vault/` must return **AccessDenied** (vaultWriter has no Delete).
3. `~/hft-bot/work/live/` EXISTS on the box (W-A1 audit B1: systemd opens the
   unit's append: log BEFORE ExecStart and does not create parent dirs —
   missing dir = status=209 crash-loop, empirically verified 2026-07-09;
   bringup_ec2.sh step 7 now creates it, re-check anyway).

### RULE — SINGLE REST OWNER (operator-locked 2026-07-08)
During the Mac↔EC2 **overlap**, the periodic **REST** tasks (catalog_sync,
dim_snapshot, build_classification, settlements, batch-orderbook cross-check —
anything that spends the shared REST **rate budget** or writes catalog/dim state)
run on **exactly one machine at any instant**. Two machines polling REST at once
= double rate-budget burn + racing catalog writers (a corruption + throttle
hazard). **WS capture concurrency is EXEMPT** — running the WS firehose on BOTH
machines at once is fine and *wanted* for the overlap diff (empirically de-risked
by the **2026-07-06 three-concurrent-connection test**).

**Cutover sequence (no instant where REST has two owners; capture never drops
because WS overlaps throughout):**
1. **EC2 starts WS capture, REST DISABLED** (WS-only; Mac remains the sole REST
   owner). EC2 **seeds its subscription universe from the S3-vaulted catalog**
   (W-A3), issuing **NO REST call** — so the single-REST-owner rule is never
   momentarily violated by a catalog fetch. No REST runs on EC2 until step 4.
2. **Verify EC2 capture health** over a soak — feed FRESH + event counter climbing
   ≥10 min, `ws_seq` continuous, zero gaps — AND confirm the **hourly raw-log
   rotation** fires correctly on EC2 across an hour boundary (the warehouse depends
   on hourly partitions; systemd must reproduce it) — before proceeding.
3. **Mac stops REST** (pause its periodic REST tasks; WS still on both machines).
4. **EC2 starts REST** (now the single REST owner).
5. **Mac `launchctl unload`** (WS + everything) — EC2 is the sole owner of WS+REST.

Rollback: through steps 1–4, WS runs on both boxes, so reversing the last step
loses NO capture. **After step 5** (Mac unloaded), rollback is not instant: it
needs `launchctl load` + reconnect + resubscribe, and the Mac's raw has a HOLE
from unload→reload that only EC2 captured — so a post-step-5 rollback must
**backfill that window from EC2/S3** (which is why W-A5 syncs EC2→S3 promptly, not
only daily, during the fragile early window). Mac launchd stays installed but
dormant throughout. (In-flight **staging DuckDB** on the unloaded Mac is safe by
D1 — it is derived from raw and rebuildable; the two boxes keep separate
raw/staging, so a partial staging is harmless.)

### ACCEPTANCE — BONUS: dual-machine capture-completeness report
While both machines run WS (the overlap window), record BOTH firehoses and **diff
them record-by-record, aligned by `ws_seq`**. Produce the project's first
**"capture completeness empirical report"**: exact cross-machine missing counts
(each direction), the **single-machine miss rate**, attributed by cause where
possible. This miss-rate becomes the **baseline for the `seq_gaps` observability
counter** (W-C5) — the counter is calibrated against ground truth, and the diff
independently proves the cutover was genuinely zero-gap.

Exit evidence:  the cutover timeline; the completeness report (miss counts +
                rate); EC2-only capture running FRESH; Mac dormant.

## W-A5 — post-cutover (steady state on EC2)
Purpose:        Make EC2 the durable, observable, cost-bounded home; land reports
                back to the operator; retire the interim Mac mitigation.
Blocked by:     W-A4.
Allowed reads:  EC2 pipeline outputs; S3.
Allowed writes: **FIRST action — a final incremental Mac→S3 sync** of the archive
                + catalog delta accumulated since the W-A3 vault (the Mac kept
                capturing between W-A3 and W-A4-step-1; that window lives only on
                Mac disk and is NOT yet in S3), re-run the byte/md5 verify on the
                delta — so "the S3 vault has the full history" is TRUE before the
                Mac is ever treated as disposable. Then: a **daily EC2→S3 sync**
                (archive + reports/ prefix) — but **prompt (sub-daily) EC2→S3 sync
                during the fragile early post-cutover window** so a post-step-5
                rollback can backfill; detectors run ON EC2 with alerts to a
                dashboard-readable file (+ operator-chosen email/webhook — **ask**);
                a **storage/cost budget section** (mandatory — S3 + EBS + egress
                monthly estimate + a cap); systemd timers.
Forbidden writes: nothing on the Mac except the report-landing job (below).

**Report flow-back (operator standing requirement, 2026-07-08):** every
daily/acceptance-class detection emits a typed **PDF report** with numbers
(playbook 3c, `tools/daily_report.py`). Post-cutover these generate ON EC2 into
`reports/`, ride the daily EC2→S3 sync under a `reports/` prefix, and a small
launchd job on the Mac runs a daily `aws s3 sync` of that prefix into
**`/Users/ritcardo/Desktop/TradingSys Report/`** — EC2 generates, S3 relays, Mac
lands. Offline Mac ⇒ reports arrive late, never lost. PDFs are derived copies; the
structured artifacts remain authoritative (D1).

**Retire the interim Mac sleep-disable (post-cutover):** once EC2 is authoritative
and all pipelines are verified healthy there, revert the Mac's interim mitigation
— `sudo pmset -a disablesleep 0` (re-enable normal Mac sleep). This is deliberately
a W-A5 step, NOT before (the Mac must stay awake as the dormant rollback host until
cutover is proven). **EXCEPTION — the W-A0 <32 GB branch:** if the daily gold build
was kept on the Mac (RAM<32 GB), the Mac is NOT fully dormant — it wakes daily to
build. In that branch do NOT fully re-enable sleep; instead schedule a daily
wake (`pmset repeat wake`) around the build and leave sleep off during it. The
≥32 GB path (gold on EC2, Mac dormant) avoids this entirely.

Acceptance:     24 h of EC2-only capture with zero gaps (the `capture_gaps`
                detector); daily S3 sync + report landing on the Mac verified once;
                cost budget within the stated cap; Mac sleep re-enabled.
Rollback:       re-activate the dormant Mac launchd (Mac still restore-capable from S3).
Exit evidence:  24 h zero-gap report from EC2; a landed PDF on the Mac Desktop;
                the cost line.

---

## Riders (operator-approved, executed within STEP 1)
- **(a) metrics.ndjson rotation** EXECUTED (512 MB, keep 3) — **on both boxes**
  (the current single unbounded `work/metrics.ndjson` is 17.8 GB; rotate it like
  the raw logs). Ships with an ingest/reader test (D4).
- **(b) promote ALL categories to `class_a_full_l1`** in the EC2 config (full-market
  L1 capture; **Exotics stay Q7-excluded from MM candidacy** — that is a *research*
  filter, not a *storage* filter, so we still capture them).
- **(c) gitignore the two pipeline-churned config CSVs**
  (`config/classification_review.csv`, `config/series_tags_report.csv`) — the
  standing BACKLOG item; stops the perpetual dirty-tree noise.
- **(d) auto-deploy for the STRATEGY process ONLY (operator requirement
  2026-07-08: "改策略期间永远不断链，一直保持数据采集").** The zero-gap guarantee
  is ARCHITECTURAL, not automation — capture and strategy are SEPARATE systemd
  units and the auto-deploy loop touches ONLY the strategy unit, NEVER capture.
  - `kalshi-capture` (ws_shadow + ingest = the data line): long-running,
    `OOMScoreAdjust=-1000`, `Restart=on-failure`, single-instance flock. Its
    restart is ALWAYS manual + off-peak + gap-accounted — **never in any auto
    loop.** (Today this is the only pipeline unit; when a live/shadow strategy
    process is added later it goes in its OWN unit — the split is what makes this
    rider real.)
  - `kalshi-strategy` (does NOT exist on the box yet — research/backtest today
    reads the warehouse OFF the hot path, so strategy work CANNOT touch capture
    now; this rider is for when a shadow/live strategy process runs ON the box):
    restarted freely on deploy.
  - Deploy mechanism: read-only GitHub **deploy key** on the box (narrowest
    secret — read-only, single-repo, no account/push reach; S4-bounded). Box
    watches a dedicated `deploy` branch (random dev commits elsewhere never
    auto-ship = a deliberate release switch). On a new `deploy`-branch commit:
    `git pull` → build → `make check` ON THE BOX → **only if green** restart
    `kalshi-strategy`; if red, DON'T restart + alert. Trigger = systemd timer
    poll (default; opens no inbound port) or GitHub webhook (instant).
  - **KEY INSIGHT (recorded for the operator):** `git pull`/build NEVER touch a
    running process; only a restart does; and only a CAPTURE restart makes a data
    gap. Strategy redeploys are therefore gap-free BY CONSTRUCTION.
  - **HARD GATE (S1/S6):** auto-restart is allowed ONLY while the strategy places
    NO live orders (research/shadow). Once it is live-order-capable, auto-restart
    of a trading process is FORBIDDEN without the lifecycle gates
    (cancel-on-disconnect proven, position reconcile) + operator per-session
    confirm — the loop must then insert an operator-ack step before the strategy
    restart. This gate ships WITH the auto-deploy unit, not later.
  - Ships with a test (D4): a dry-run proving a RED `make check` does NOT restart
    the unit. Build target: W-A5 (steady state); the capture-unit split can land
    at W-A1.
- **(e) notification / alerting system (operator requirement 2026-07-09).** This
  specifies W-A5's alert delivery (the "operator-chosen email/webhook — ask").
  - **PRIMARY channel = Telegram** (bot via @BotFather → bot token lives in
    `~/.kalshi/env.sh`, operator-created, S4; an alert = a simple HTTPS POST to the
    Bot API — reaches the operator's phone anywhere). **Backup = email**
    (Lyz2003@protonmail.com). **At-console history = the dashboard alert stream.**
  - **INFRA layer (AWS CloudWatch alarms → SNS):** instance status-check fail /
    unreachable, **disk usage > threshold (e.g. 80%)** — fires EVEN IF the app is
    dead. SNS→email needs an operator email-subscription confirm (S4).
  - **APP layer (pipeline detectors: capture_gaps W-C2, incident detector W-D3):**
    capture gap / feed stale / reconnect storm, clock skew, **monthly cost over the
    S3+EBS+egress cap**.
  - **LATER (execution engine, STEP 6+):** fills, position-limit breach,
    kill-switch, risk events fan out through the SAME Telegram/email path —
    extended, not rebuilt.
  - Built at W-A5 (post-cutover, on EC2); read-only, off the hot path (S5). The
    Telegram bot token + email SNS confirmation are operator actions (S4).

## §6 self-audit (this plan vs GUARDRAILS)
1. Phase/gates (P1,P2): infra for Phase 1→2; skips no gate; enables the 7-clean-days gate. ✅
2. Live orders (S1–S6): none. Credentials operator-only (S4); cutover operator go/no-go. ✅
3. Log-odds + fees (Q1,Q3): n/a (infra). ✅
4. Pessimistic bound (Q2): unaffected; this PROTECTS it by giving it a holed-free tape. ✅
5. WS trading data (Q5): the WS capture path is preserved and **relocated AND
   EXPANDED** — rider (b) promotes all categories to full-market L1, so W-A0
   sizing must carry the larger subscription universe (noted in W-A0). ✅
6. Tests incl. behavior (E1,D4): W-A2 = full green on the box; riders ship tests. ✅
7. Pipeline continuity (P4): the whole plan is built around zero-gap; WS overlaps
   both boxes; Mac stays dormant-recoverable; S3 restore proven BEFORE cutover. ✅
8. Reversible/bounded (P3,P6): every W reverts (terminate box / delete S3 versions /
   re-enable Mac launchd); no Mac data destroyed until EC2 is authoritative. ✅
9. Docs move with code (E5): deploy/ units + RUNBOOK EC2 section + schema notes in-change. ✅
10. Could green lie (D2): W-A4 completeness diff + W-A5 24 h zero-gap detector make
    a "cutover looked fine" claim falsifiable with real numbers. ✅

## Execution order + operator prerequisites
W-A0 → W-A1 → W-A2 → W-A3 → **W-A4 (operator go/no-go)** → W-A5, one W per fresh
session, independent audit after each. **Only the operator can:** create the AWS
account, launch the instance, create IAM + the S3 bucket, hand-create
`~/.kalshi/env.sh` on the box (S4), and be present for the W-A4 cutover. The agent
does everything else (bring-up scripts, systemd units, validation, sync + restore
scripts, the cutover runbook) — but never touches account or credentials.
