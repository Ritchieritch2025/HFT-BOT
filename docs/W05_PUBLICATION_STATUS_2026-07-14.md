# W05 publication status — 2026-07-14 (session verdict)

**一行裁决(经操作员 2026-07-14 修正令,见文末 AMENDMENT):✅ W05_ACCEPTED。**
发布成功(07-12 + 07-13 双日,版本绑定,Mac 端 verify 全 PASS);操作员以
option 2 修订验收标准 —— 评估器落地前,"降级原因仅剩 W03-pending"的 release
以显式层级 `SEALED_PENDING_QUALITY_ASSESSMENT` 记名,满足 W05_ACCEPTED,
限 EXPLORATORY(Track A)研究使用且每个产物带层级横幅;VERDICT 级结论仍需
评估器建成回填后的完整 SEALED_CONFIRMATION。

> 本文其余部分是修正令之前的原始裁决记录,保留不改(历史)。原
> "唯一 blocker" 分析仍然准确 —— 它正是修正令所裁决的对象。

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
