# W05 publication status — 2026-07-14 (session verdict)

**一行裁决(经操作员 SUPERSEDE 令,2026-07-14 深夜,见文末 SUPERSEDE 节;
它取代此前的 option-2 AMENDMENT):⚠️ W05_ACCEPTED 已 REVOKED。**
现行状态 = **W05_EXPLORATORY_READY(待 RFQ 重发布后正式返回)**:
- 正式验收保持严格:PIPE-W03 评估质量之前,任何层级都不得视同
  SEALED_CONFIRMATION;
- 07-12/07-13 现有 release 仅够探索级;层级横幅纪律保留 —— Tier-1
  描述性探索用途,**禁止** freeze / verdict / promotion / live-candidate
  任何一类主张;
- RFQ GAP:先前授权"sealed RFQ inclusion ON"生效 —— 现有两个
  RFQ-excluded release 不满足 RFQ 研究范围,须带 sealed RFQ 重发布
  (state-aware 新 release id)+ Mac 端 inventory/fetch/exact-VersionId
  verify 通过,才返回 W05_EXPLORATORY_READY。

> 本文下方依序保留:原始裁决记录(历史)→ option-2 AMENDMENT(已被
> SUPERSEDE 取代,留档)→ SUPERSEDE 原文逐字 + 落地记录(现行权威)。

## 已完成(本 session,操作员 2026-07-14 启动令授权的 retry)

B12(IAM 缺 s3:GetObjectVersion)已由操作员关闭,验证有效:发布器的
post-upload 逐对象 GetObject --version-id 校验全部通过。

| 日期 | release_id | 对象数 | 大小 | binding | Mac verify |
|---|---|---|---|---|---|
| 2026-07-12 | `2026-07-12__seal-bc37de4c__pub-3e9c7603b8cab292` | 335 | 3.24 GB | VERSION_BOUND (manifest ver `QO6vnhHrfPFf0BVbUwiTHo9QatbR4nmA`) | **PASS** |
| 2026-07-13 | `2026-07-13__seal-7f6e5c1b__pub-be44d2be5e80e7fd` | 330 | 2.71 GB | VERSION_BOUND (manifest ver `VVwPg9M9DusmDSMQ5aVgjODxPoTsnLqN`) | **PASS** |

- 发布命令:EC2 上 `tools/research_release.py publish --date <D>
  --operator-approved`(授权出处 = 操作员本 session 启动令第 2 条
  "retry the research release publication for 2026-07-12 AND 2026-07-13
  (B12 closed)";未建常驻 arm-file,一次性、范围仅此两日)。
- Mac 端 no-SSH 验证:`sandbox/w05-recovery-fix/tools/research_data.py`
  inventory → fetch → verify,只读 SigV4 凭据,两 release 均
  tier=SEALED_DEGRADED_EVIDENCE / tl1=TL1 / L2=INCLUDED_SEALED_FACTS /
  RFQ=EXCLUDED_PENDING_OPERATOR_COST_ACK。
- S3 现状(inventory 实测):4 个 release 前缀共 11.99 GB,存储 ≈$0.28/月。
  其中 1 个是 07-12 的 TORN 残留 `…pub-e1007e36c3cd927b`(无 MANIFEST,
  永不曝光,3.24 GB)— 是否清理由操作员定(D1 只保护已发布 release;
  这个从未发布)。

## W05_ACCEPTED 的一道精确 blocker

PIPE-W05-SPEC §20 要求 evidence tier = **SEALED_CONFIRMATION**,而
ADDENDUM 4(操作员终审判决,2026-07-12)明令
"UNASSESSED_PENDING_PIPE_W03 must never earn SEALED_CONFIRMATION"。
现实:`tools/export_day.py:525` 给每一个 full_v2 seal 硬编码写入
`capture_quality_status = UNASSESSED_PENDING_PIPE_W03` —— 采集质量评估器
(PIPE-W03)还没建。因此**任何**现行 seal 的发布都只能是
SEALED_DEGRADED_EVIDENCE(本次两日的降级原因即此一条,别无其他:
gap receipt affirmative ✓、L2 evidence ✓、full_v2 ✓、go_no_go_eligible ✓)。

⇒ **W05_ACCEPTED = BLOCKED,唯一路径由操作员选:**
1. 建 PIPE-W03(采集质量评估),让 seal 带上真实评估 → 重发布拿
   SEALED_CONFIRMATION;或
2. 操作员修订 §20 的 Phase-A 验收标准,接受"降级原因仅剩 W03-pending"
   的 SEALED_DEGRADED_EVIDENCE;或
3. 维持 BLOCKED 等 W03。

次要未满足项(同属 §20,操作员成本决定):RFQ 纳入是成本开关
(+$21–23/月复利),需操作员 cost ack 后带 `--include-rfq` 重发布
(state-aware:会产生新 release id,旧的不动)。

## 对 auto-research 的影响

SPORTS-AUTORESEARCH-01 的 GATE A 判 FAIL 条件 = "默认 confirmation-only
研究视图为空(零 VERSION_BOUND **SEALED_CONFIRMATION** release)"。
两个新 release 是 VERSION_BOUND 但 DEGRADED —— 按任务书字面,GATE A
仍然 FAIL,直到上面 1/2 任一落地。操作员批 W09/重发任务时请把这一条
一并裁决(否则任务还会在 GATE A 停)。

---

## AMENDMENT — 操作员修正令(原文逐字,2026-07-14)

```
OPERATOR RULINGS:
1. B18 approved — I will run: sudo loginctl enable-linger ubuntu
   (durable fix = migrate sync to a system-level unit; add to debt list.)
2. W05 acceptance AMENDED (option 2): until the capture-quality assessor
   ships, releases carry the explicit tier SEALED_PENDING_QUALITY_ASSESSMENT.
   This tier satisfies W05_ACCEPTED and is valid for EXPLORATORY (Track A)
   research with the tier banner on every artifact. VERDICT-grade claims
   still require full SEALED_CONFIRMATION after the assessor exists and
   backfills. Record this amendment in the status doc and STATE, then
   return W05_ACCEPTED.
3. The 3.2 GB unexposed publication debris: KEEP, tag it in the status doc,
   clean up in the next housekeeping pass. No manual S3 deletions.
4. W-A: proceed to independent audit (fresh session), deploy only on PASS.
```

## 修正令落地记录(同 turn,E2)

- **W05_ACCEPTED 正式返回**,依据 = 上述第 2 条。两个 release
  (`2026-07-12__seal-bc37de4c__pub-3e9c7603b8cab292`、
  `2026-07-13__seal-7f6e5c1b__pub-be44d2be5e80e7fd`)按修正令记名为
  `SEALED_PENDING_QUALITY_ASSESSMENT`(逻辑层级 —— S3 上的 MANIFEST 是
  不可变对象,内嵌字符串仍是发布时的 `SEALED_DEGRADED_EVIDENCE` +
  downgrade_reasons=[仅 W03-pending];消费端以本修正令 + 状态文件为准)。
- **随之产生的代码债(记入分支 BACKLOG,下一个维护 W 处理):**
  - B20:research_release.py `derive_evidence_tier` 在
    downgrade_reasons == [W03-pending 一条] 时应直接产出
    `SEALED_PENDING_QUALITY_ASSESSMENT`(新发布自带正确层级);
  - B21:research_data.py 默认视图/横幅逻辑:该层级 = Track A 可用 +
    强制横幅;VERDICT 级仍只认 SEALED_CONFIRMATION;
  - B18 长效解:sync 迁移到 system-level unit(不再依赖 user manager)。
- **TORN 残留处置(第 3 条):** `2026-07-12__seal-bc37de4c__pub-e1007e36c3cd927b`
  (335 对象,3.24 GB,无 MANIFEST,从未曝光)—— **KEEP**,标记为
  `HOUSEKEEPING_PENDING`,下次 housekeeping pass 统一清理;禁止手工 S3 删除。
- **GATE A 影响更新:** 修正令后,confirmation-only 默认视图对 Track A
  (EXPLORATORY)等价于 "SEALED_PENDING_QUALITY_ASSESSMENT 可见 + 横幅";
  SPORTS-AUTORESEARCH GATE A 的字面判据是否随之改写,归属任务重发时的
  操作员定稿(建议随 B21 一并处理)。

---

## SUPERSEDE — 操作员严格化令(原文逐字,2026-07-14 深夜;取代上方 option-2 AMENDMENT)

```
SUPERSEDE — the option-2 draft you executed was replaced by a stricter
operator ruling. Corrections, in order:

1. RENAME the acceptance: W05_ACCEPTED is REVOKED. The 07-12/07-13 releases
   are W05_EXPLORATORY_READY only. Formal acceptance stays strict: no tier
   may be treated as SEALED_CONFIRMATION until PIPE-W03 assesses quality.
   Update STATE and docs/W05_PUBLICATION_STATUS_2026-07-14.md accordingly
   (archive this supersede verbatim). Tier-banner discipline you implemented
   stays — Tier-1 descriptive exploratory use only; no freeze/verdict/
   promotion/live-candidate claims.
2. RFQ GAP: prior authorization "sealed RFQ inclusion ON" is in force.
   The current RFQ-excluded releases do NOT satisfy the RFQ research scope.
   Publish new state-aware releases for 07-12/07-13 WITH sealed RFQ included,
   prove Mac/W09 inventory + fetch + exact-VersionId verify, then return
   W05_EXPLORATORY_READY.
3. B18: already executed by operator (Linger=yes; rollback recorded:
   sudo loginctl disable-linger ubuntu). VERIFY the mechanism: if the hourly
   sync is NOT a systemd --user unit but an SSH-spawned process, linger is
   not the cure — the durable fix is migration to a system-level unit
   (registered debt). Confirm which it is with evidence.
4. Unchanged: keep B20/B21 registrations, HOUSEKEEPING_PENDING tag, and the
   W-A audit block; W-A deploys only after tonight's 07-14 seal passes.
```

## SUPERSEDE 落地记录(执行中,完成项随做随记)

- [x] W05_ACCEPTED 撤销;本文头部改判 + STATE 行改写(W05 =
  EXPLORATORY_ONLY,严格验收门保留)。
- [x] RFQ 重发布 07-12 + 07-13(--include-rfq,state-aware 新 id)→ 结果表
  见下方 "RFQ 重发布结果" 节(双日 INCLUDED_SEALED_RAW,发布器全对象校验过,
  Mac inventory EXPOSED;剩 W09 fetch+verify)。
- [x] B18 机制取证 → 见下方 "B18 机制核验" 节(system unit + snap scope,
  linger 有效,长效债改写)。
- [x] 第 4 条不变项确认:B20/B21 在册(分支 BACKLOG)、HOUSEKEEPING_PENDING
  在档、W-A 审计令在 SESSION_LOG 23:40 条目;W-A 部署顺序 = 今晚 07-14
  封印 PASS 之后 + 审计 PASS。

## B18 机制核验(SUPERSEDE 第 3 条,证据在案)

**结论:两者都不是 —— sync 既不是 systemd --user unit,也不是 SSH 起的进程,
而是正规 system-level unit;被杀的是它的子进程 `aws`(snap 版)。Linger 有效,
但长效债需改写:问题不在 unit 层级,在 snap。**

证据链(全部实测):
1. unit 是 system 级:`/etc/systemd/system/kalshi-s3-sync-hourly.service`
   (Type=oneshot, User=ubuntu, ExecStart=deploy/ec2_s3_sync.sh hourly)+
   同名 system timer(OnCalendar=*:05)。`systemctl cat` 输出在案。
2. 但脚本里的 `aws` = snap aws-cli:`which aws` → `/snap/bin/aws` →
   `aws-cli.aws`(snap aws-cli 2.35.21, classic)。`snap run` 会把 aws
   进程注册进一个 transient scope,而这个 scope 挂在 **user@1000.service
   (用户管理器)** 下面 —— 不在 system service 自己的 cgroup 里。
3. 两次被杀的 journal 完全同构:
   - 21:05:45:`systemd[1]: Stopping user@1000.service` →
     `systemd[283652](用户管理器): Stopping snap.aws-cli.aws-6f52b35e….scope`
     → `bash[285417](sync 的 shell): Terminated`
   - 22:05:24:同链条,scope id `…ea714b00…`,`bash[339782]: Terminated`
   触发条件都是"最后一个 SSH 会话登出 → logind 收掉用户管理器"。
4. `Linger=yes` 已实测在案(操作员已执行;回滚命令已记录)。Linger 让
   user@1000 常驻 ⇒ scope 不再随登出被收 ⇒ **对本故障是有效解**。
5. **残余脆弱性(所以长效债保留但改写):** 任何令 user@1000 重启的事件
   (手工 restart、用户管理器崩溃、systemd 升级 re-exec)仍会杀掉进行中的
   aws。长效修复不是"迁 system unit"(它已经是),而是**去掉 snap/用户
   管理器依赖**:非 snap 的 aws v2(官方安装包)或改用自带 SigV4 的
   python 上传器。分支 BACKLOG 的 B18 长效项已按此改写。

## RFQ 重发布结果(SUPERSEDE 第 2 条,2026-07-15 00:4xZ 完成)

| 日期 | release_id(新,state-aware) | 对象数 | 大小 | rfq | binding | 发布器逐对象校验 |
|---|---|---|---|---|---|---|
| 2026-07-12 | `2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03` | 485 | 35.59 GB | **INCLUDED_SEALED_RAW** | VERSION_BOUND (manifest ver `JLvZOATi.HIwBKlAj6Auej4apdfiE_fe`) | 485/485 |
| 2026-07-13 | `2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5` | 476 | 32.51 GB | **INCLUDED_SEALED_RAW** | VERSION_BOUND (manifest ver `1ZFrFjSr2UbFODHEsiw_MfsbBOM1bKxY`) | 476/476 |

- Mac inventory(只读,00:4xZ):两个新 release 均 **EXPOSED**,
  rfq=INCLUDED_SEALED_RAW;S3 research/ 现量 80.09 GB(≈$1.84/月,
  增速 ~26.7 GB/日 ≈ +$18.4/月/月,与操作员 +$21–23 估算同量级)。
- 注:RFQ raw 重建自 vault 时有 "no object versions" 的 DEGRADED
  unversioned recovery 警告(内容仍逐字节对封印 sha256 校验)——vault
  ec2/raw 前缀对该凭据不可见版本号,后续 housekeeping 可查
  是否给 vaultWriter 补 ListBucketVersions on ec2/raw(非阻塞)。
- 旧的 RFQ-excluded 两个 release 按 D1 保留不动(EXPOSED,可继续用于
  无 RFQ 的轻量拉取)。
- **W05_EXPLORATORY_READY 的最后一步:** W09 验收(操作员开机后)兼做
  exact-VersionId fetch+verify(Mac 按裁决只做 inventory)。验收 PASS
  即正式返回 W05_EXPLORATORY_READY。
