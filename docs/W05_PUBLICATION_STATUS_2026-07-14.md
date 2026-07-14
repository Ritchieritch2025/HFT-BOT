# W05 publication status — 2026-07-14 (session verdict)

**一行裁决:⚠️ 发布成功(07-12 + 07-13 双日,版本绑定,Mac 端 verify 全 PASS),
但 W05_ACCEPTED 还差一道结构性门:证据层级到不了 SEALED_CONFIRMATION —
唯一精确 blocker = PIPE-W03 采集质量评估还不存在。**

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
