# W-PUB-REF-01 — Research Zero-Copy Reference Releases

**Status:** PROPOSED — awaiting independent audit and operator ratification  
**Date:** 2026-07-16  
**Priority:** P0, Research-Access Track  
**Authority granted by this document:** none. This document is a plan, not an
execution order. It authorizes no AWS, IAM, lifecycle, production, instance, or
code mutation.

**Revision note:** RFQ remains supported as sealed-only explicit opt-in. The
terminated `DATA_INTEGRITY_BLOCKED` repair branch remains closed.

## 1. 小学生版本

现在的做法像这样：生产仓库里已经有一本书，为了让研究机器读它，
我们又复印一本放进 `research/`。这会重复占 S3 空间，也让发布很慢。

本计划改成：

1. 生产端给每一本已经封好的书做一张不可伪造的“借书卡”；
2. `research/` 只放一张很小的书单，不再放第二本书；
3. 研究机器只能按书单上的精确版本借阅，只读，不能改、不能删；
4. 下载到研究机器的临时缓存可清理，但 S3 的原件不动；
5. 新方式出错就立即退回已经验证的旧 v2 副本。

人工启动和停止 W09 不变，研究程序看到的本地目录形状也不变。
RFQ 也没有被删掉：合格的 sealed RFQ 仍可按开关读取，只是不再复制，
也绝不借这个改造重开已经终止的修复分支。

## 2. 本 W 要解决的问题

当前 v2 发布会把 facts、dim、catalog、quality 等对象复制到
`research/releases/<release_id>/`。现有 W09 consumer 又硬编码从该位置
下载。因此，只改 IAM 或只写一个 manifest 都不能完成零复制。

目标状态：

```text
生产采集/封存                  小型控制面                   W09 研究机
ec2/warehouse/...       -> research/releases/...     -> exact VersionId GET
唯一一份大数据               只有 MANIFEST.json           本地校验缓存 + 旧目录视图
```

本 W 同时补齐三个缺件：

- canonical control plane 中的精确版本收据；
- v3 reference publisher；
- 双版本 W09 consumer 与最小只读 IAM。

## 3. 范围和非范围

### 3.1 范围内

- 已封存的 L1、L2、盘口 facts；
- 对应的 seal、日期化 dim/catalog snapshot、warehouse manifest；
- corrections、gap receipt、L2 quality 和其他日期化质量证据；
- 符合现行 evidence policy、未被 quarantine/`DATA_INTEGRITY_BLOCKED` 的
  sealed RFQ 精确版本，保持显式 opt-in；默认确认视图仍要求
  `SEALED_CONFIRMATION`，degraded evidence 只有任务书明确授权时才可见；
- canonical 上传的逐对象 VersionId、size、SHA-256 收据；
- v2 copied release 与 v3 reference release 并存；
- W09 本地 content-addressed cache 和相同 logical view；
- shadow、canary、rollback 和独立审计。

### 3.2 第一版硬排除

- 除获准 sealed RFQ 精确版本外的所有 `ec2/raw/*`；
- 已终止 RFQ 分支及其 repair/replay/re-audit/re-test，以及已标记
  quarantine/`DATA_INTEGRITY_BLOCKED` 的 RFQ 对象；
- 正在写入的 firehose/L2 active-hour 文件；
- `latest`、glob、空 VersionId 或 versionless canonical GET；
- 自动启动/停止 W09；
- S3 lifecycle、Object Lock、存储层迁移；
- 新 Telegram 功能；
- 修改 capture、ingest、seal 的数据语义；
- 删除、覆盖或迁移任何既有 v2 release。

RFQ 的“读取能力”和“修复分支”必须分开：本 W 保留前者，但不重开后者。
RFQ 只有在 seal、receipt、exact VersionId/size/SHA 和 eligibility tags 全部
PASS 后，才可由 manifest 引用；consumer 仍需显式 `--with-rfq` 才会下载。

## 4. 不可妥协的安全约束

1. **一份事实数据。** 新 v3 release 不得向 `research/` 上传 parquet、
   ndjson、质量证据副本或其他数据对象；release prefix 只允许一个
   `MANIFEST.json`。receipts 留在 canonical control plane。
2. **精确版本。** canonical 引用必须同时绑定 bucket、key、非空
   VersionId、size 和完整 SHA-256；ETag 不能替代 SHA-256。
3. **先验证，后曝光。** 任一对象缺失、hash 不符、未封存、未获准或
   权限不足，都不得写出可发现的 v3 `MANIFEST.json`。
4. **热路径不等待。** sync、receipt、tag、publish 或研究失败只能令
   研究发布进入等待/阻断态；不得阻塞 capture、ingest 或 seal。
5. **研究机只读。** W09 对生产数据无 List、Put、Delete、Tagging、
   Restore、Lifecycle 或 EC2 control-plane 权限。
6. **raw 默认不可读。** 基础 W09 role 读取 active、未封存、blocked 或非
   RFQ 的 `ec2/raw/*` 必须得到 403。唯一例外是 manifest 点名、seal/receipt
   已验证、带双重 eligibility tags 的 sealed RFQ exact VersionId。
7. **旧路不拆。** canary 与 rollback 完成前，v2 publisher/consumer 保持
   可用；旧 v2 release 永久只读，不原地改成 v3。
8. **禁止误删。** receipt 缺失、损坏或冲突时，local raw prune 的动作
   必须是“保留并告警”，绝不能退回 seal-only 删除。

## 5. 两种 release 的固定合同

| 模式 | S3 数据位置 | `research/` 内容 | W09 读取方式 |
|---|---|---|---|
| `COPIED_V2` | research release 内的副本 | manifest + 全部数据 | 现有路径，保持不变 |
| `REFERENCE_V3` | canonical 精确版本 | 仅一个 `MANIFEST.json` | 按 manifest 的 VersionId 读取 |

inventory 必须明确显示 `COPIED_V2` 或 `REFERENCE_V3`，不能让操作员猜。

### 5.1 v3 release identity

固定为新的 v3 namespace：

```text
<date>__v3ref__seal-<seal_sha256前8位>__pub-<publication_state_sha256前16位>
```

`reference_set_sha256` 只证明数据对象地址与字节绑定；另计算覆盖完整、稳定
发布语义的 `publication_state_sha256`。对象集合、对象语义、correction、gap
evidence、quality evidence、TL1、evidence tier/basis、RFQ 开关或其他绑定
变化时，必须产生新 release ID。禁止覆盖旧 ID。

### 5.2 v3 manifest 最小 schema

```json
{
  "schema": "research-release-manifest-v3-reference",
  "schema_version": 3,
  "storage_mode": "CANONICAL_REFERENCE",
  "release_id": "2026-07-13__v3ref__seal-1234abcd__pub-5678ef901234abcd",
  "date": "2026-07-13",
  "publication_status": "PUBLISHED",
  "rfq_policy": "OPTIONAL_SEALED_ONLY",
  "rfq_included": false,
  "published_at_utc": "<RFC3339 UTC>",
  "publisher_commit": "<git commit>",
  "source_seal": {
    "bucket": "<canonical bucket>",
    "key": "ec2/warehouse/seals/<date seal>",
    "version_id": "<non-empty S3 VersionId>",
    "size": 123,
    "sha256": "<64 hex>",
    "verification_state": "PASS"
  },
  "reference_set_sha256": "<64 hex>",
  "object_semantics_sha256": "<64 hex>",
  "publication_state": {
    "schema": "research-release-manifest-v3-reference",
    "storage_mode": "CANONICAL_REFERENCE",
    "date": "2026-07-13",
    "source_seal_binding_sha256": "<64 hex>",
    "reference_set_sha256": "<64 hex>",
    "object_semantics_sha256": "<64 hex>",
    "evidence_tier": "<existing evidence tier>",
    "evidence_basis_sha256": "<64 hex>",
    "corrections_digest": "<64 hex>",
    "gap_evidence_digest": "<64 hex>",
    "l2_quality_digest": "<64 hex>",
    "tl1_status": "<existing TL1 state>",
    "rfq_policy": "OPTIONAL_SEALED_ONLY",
    "rfq_included": false
  },
  "publication_state_sha256": "<64 hex>",
  "evidence": {
    "tier": "<existing evidence tier>",
    "basis": "<existing evidence basis>"
  },
  "objects": [
    {
      "logical_key": "warehouse/facts/<date-partitioned-object>",
      "source_bucket": "<canonical bucket>",
      "source_key": "ec2/warehouse/facts/<date-partitioned-object>",
      "source_version_id": "<non-empty S3 VersionId>",
      "size": 123,
      "sha256": "<64 hex>",
      "kind": "facts",
      "channel": "orderbooks_l1",
      "date": "2026-07-13",
      "required": true
    }
  ]
}
```

`reference_set_sha256` 必须对按 `logical_key` 排序后的以下元组进行规范化
编码后计算：

```text
logical_key, source_bucket, source_key, source_version_id, size, sha256
```

规范化规则必须在 schema 中冻结：UTF-8、object keys 排序、objects 按
`logical_key` 排序、固定分隔符与换行规则，并拒绝重复 logical key。时间戳和
本地路径不参与 `reference_set_sha256`。
`source_seal` 必须与 objects 中对应 seal entry 的 bucket/key/VersionId/
size/SHA 完全一致；不一致时拒绝 manifest。

`object_semantics_sha256` 对每个对象的 `logical_key/kind/channel/date/required`
及其 seal/evidence binding 做稳定排序和 hash。`publication_state_sha256` 对
上例 `publication_state` 的规范化对象计算，并至少覆盖 schema、storage
mode、seal binding、reference set、object semantics、evidence tier/basis、
corrections、gap/L2 quality、TL1、RFQ policy/inclusion。published timestamp、
本地路径和运行 ID 不参与。顶层便捷字段必须与 `publication_state` 相同，
不一致即拒绝。

v3 还必须保留现有 v2 对 seal、evidence tier/basis、corrections、gap/L2
quality receipts、TL1 和 publication state 的完整语义。实现 agent 不得以
上述“最小 schema”删除旧完整性字段。

RFQ entry 另有固定约束：`logical_key` 使用 `raw_rfq/<seal-relative-path>`；
S3 `source_key` 则是 `ec2/raw/<seal-relative-path>`，实际形状为
`ec2/raw/date=YYYY-MM-DD/rfq_<HH>.ndjson[.<segment>]` 或
`rfq_receipts_<HH>.ndjson[.<segment>]`。basename 必须匹配当前 `_RFQ_RE`，
而且完整相对路径必须逐字出现在权威 seal 的 `raw_files`；禁止靠 glob 自行
发现。RFQ entry 还必须是 `kind/channel=rfq`、`required=false`，并携带 seal
entry binding、evidence tier 和 integrity state。跨日文件只有 seal 明列时
才允许，路径日期不强行改写成 release date。`rfq_included=false` 时 objects
中不得出现 RFQ；`rfq_included=true` 时 RFQ references 必须参与
`reference_set_sha256`，不能成为未绑定的附加列表。

## 6. Canonical receipt 合同

### 6.0 实施前置检查

- canonical bucket 的 S3 Versioning 必须为 `Enabled`，不能是 `Suspended`；
- research manifest 所在 bucket 的 S3 Versioning 也必须为 `Enabled`；
- 任一数据或 manifest 的 VersionId 为 `null`/空值时立即阻断；
- 实现 agent 必须先确认 authoritative production repository 和 commit，
  不能把 sandbox 副本的修改误当成已部署代码；
- 先盘点现有 lifecycle 与对象 tags，只读记录，不在 01A 中顺手改策略。
- 先盘点所有拥有 `PutObjectTagging` 或 `PutObjectVersionTagging` 的
  automation principals；不能证明 eligibility tag 只有一个 exact-version
  writer 时，禁止进入 tag phase。

### 6.1 位置与粒度

不要求每个数据对象再创建一个小文件。允许一个不可变的 daily receipt
包含多条逐对象记录。建议控制前缀：

```text
ec2/control/canonical-receipts/v1/date=YYYY-MM-DD/
  receipt-<receipt_set_sha256>.json
```

同一内容重跑应命中同一 digest key 并验证复用，不产生新的数据版本。
receipt 必须 conditional-create、按返回的自身 VersionId 回读并校验 hash。
`receipt_set_sha256` 必须来自稳定 canonical projection：schema、date、seal
binding，以及按 key 排序的 bucket/key/VersionId/size/SHA、规范化 seal-relative
logical source key、source kind/table/channel/date、durability、eligibility 和
evidence-binding；host absolute path、`verified_at_utc`、运行 ID 和 code commit
不参与 identity。key 已存在时，只有现存 receipt 的完整 stable projection 与
本次请求逐字段相等才可直接回读复用；不得重新生成新时间戳。
完整对象 SHA 是首次 attestation 的一次性成本：同一 bucket/key/VersionId 已有
不可变、回读 PASS 的 receipt 时，后续 release 只复核 receipt、对象存在性和
tag/storage state，不再次整对象下载或重复打 tag。若任一绑定变化，fast path
按变化类型处理：bucket/key/VersionId/size/SHA 数据绑定变化必须重新完整验证；
仅 eligibility/evidence 状态变化时，可以复用原不可变 exact-version byte
attestation，生成新的 receipt-set digest，无需重读全部字节。若发现未授权的
tag 或 storage drift，则冻结发布并告警，不静默“修回去”。

### 6.2 每个对象的必填字段

```text
bucket
key
VersionId
size
SHA-256
logical source path/key
source kind / table / channel / date
seal SHA-256 或 manifest digest 绑定
verified_at_utc
publisher code commit
durability_verified
research_eligible
eligibility tag state
```

receipt 可以记录 raw 的“已耐久上传”状态供 local prune 使用。普通 raw、
active raw、blocked/quarantined RFQ 必须是 `research_eligible=false`。只有
seal 和完整性证据均 PASS 的 exact RFQ version 可以是
`research_eligible=true`，并且仍然是 manifest/consumer 双重 opt-in。

### 6.3 生成顺序

1. 从已验证 seal 和日期化质量证据构造精确对象白名单；禁止 glob/latest。
2. canonical sync 仅处理已经关闭、大小稳定、不会继续写入的对象。
3. 获取上传结果的 VersionId，并用该 exact VersionId 完整回读。
4. 校验 size 和 SHA-256；multipart ETag 只能作诊断信息。
5. 对允许研究引用的 warehouse/control 精确版本，合并并保留既有 tags 后
   添加 `research-eligible=true`。对获准 sealed RFQ 还必须同时添加
   `research-channel=rfq`；其他 raw 永不添加研究 eligibility tag。
6. 再验证 exact-version 可读性和 tag 状态。
7. conditional-create receipt，回读自身 exact VersionId 并校验。
8. receipt 已在 S3 耐久落盘后，才原子更新本地 receipt index；更新方式必须
   是 temp + fsync + rename 或等价事务。
9. local prune 只读本地 index，不在删除路径同步调用 S3。

S3 tagging 是整组替换且没有 `If-Match` CAS，所以本计划不声称能在多 writer
下检测并阻止所有竞争。进入 tag phase 的前提是 IAM/bucket policy 同时拒绝
其他 automation principals 的 `PutObjectTagging` 和
`PutObjectVersionTagging`，并只把后者按限定资源授予唯一 dedicated tagger。
tagger 自身不使用、不需要 versionless `PutObjectTagging`；它串行执行
GET-all-tags(exact VersionId) -> merge -> PUT(exact VersionId) -> GET-verify。
若无法建立 single writer、tag 已达数量上限、发现未知 writer 或巡检发现 tag
drift，立即 fail closed；必须修订为不可变 eligibility registry 等替代设计，
不能冒险覆盖 tags。

### 6.4 canonical readiness 状态机

```text
SEALED
  -> PENDING_CANONICAL
  -> RECEIPT_VERIFIED
  -> REFERENCE_MANIFEST_PUBLISHED
  -> CANARY_VERIFIED
  -> DEFAULT_V3

任一步失败 -> RETAIN_AND_ALERT / BLOCKED_INTEGRITY
             capture、ingest、seal 继续运行
```

seal 完成不等于 canonical ready。publisher 在 receipts 不齐时必须返回
`PENDING_CANONICAL` 并由异步重试处理，不能让 seal 或 capture 等待。

## 7. Publisher 合同

v3 publisher 只接受满足以下全部条件的对象：

- seal verification 为 PASS；
- canonical receipt 中存在 exact VersionId、size 和 SHA-256；
- 对象属于固定 allowlist；若为 RFQ，`source_key` 必须匹配
  `ec2/raw/date=*/rfq_*.ndjson*` 或
  `ec2/raw/date=*/rfq_receipts_*.ndjson*`，basename 匹配 `_RFQ_RE`，完整相对
  路径又逐字存在于 seal `raw_files`，且 seal confirmation、receipt、非
  quarantine/blocked 状态全部 PASS；
- `research_eligible=true` tag 已对同一精确版本复核；
- RFQ 还必须复核同一精确版本的 `research-channel=rfq` tag，且只有显式
  `--include-rfq` 发布才可进入对象集合；
- gap、L2 quality、corrections、dim/catalog snapshot 等必需证据齐全。

发布动作：

1. 从 receipts 构造确定性对象集合；
2. 对每项再做 exact VersionId、size、SHA 和前缀校验；
3. 生成规范 JSON 和 release ID；
4. 确认 release prefix 内没有数据对象；
5. 以 `If-None-Match: *` conditional-create 写
   `research/releases/<release_id>/MANIFEST.json`；
6. 取得 MANIFEST 自身 VersionId，exact-version 回读并校验 manifest SHA；
7. 只在全部成功后报告 `REFERENCE_MANIFEST_PUBLISHED`。

重复发布相同输入必须是 no-op：上传大数据字节为 0，不能创建第二份事实
对象，也不能给同名 MANIFEST 制造新版本。

## 8. W09 consumer 与本地 cache

consumer 按 `schema/storage_mode` 分派：

- v2：保持现有 copied-release 逻辑；
- v3：只使用 manifest 中明列的 exact canonical references。

v3 下载前必须拒绝：未知 bucket、未知 prefix、非 RFQ raw、未获准 RFQ、
路径穿越、重复 `logical_key`、空 VersionId、与 seal binding 不一致的日期/
相对路径、未知 kind 或 schema。
RFQ 对象即使出现在合格 manifest 中，也只有 `--with-rfq` 时才下载。

本地存储规则：

```text
/srv/w09-research/cache/objects/sha256/<前2位>/<完整sha256>
/srv/w09-research/releases/<release_id>/<logical_key>  -> cache symlink
```

- 下载使用 `.part -> size/SHA verify -> atomic rename`；
- cache 命中必须复核 SHA，不能只比较文件大小；
- 损坏缓存移入 quarantine 后重新拉取；
- 相同 SHA 在不同 release 间只保存一份本地数据；
- 所有 required objects 通过后才写 `.VERIFIED.json`；
- `.VERIFIED.json` 必须绑定 MANIFEST VersionId、manifest SHA 和
  `reference_set_sha256`；
- marker 必须区分 `rfq_state=NOT_FETCHED_OPT_IN` 与
  `rfq_state=VERIFIED_SEALED_RAW`，不得把“未下载 RFQ”写成“RFQ 已验证”；
- logical tree 和现有 warehouse-shaped view 保持一致，研究 runner 不需要
  知道来源是 v2 还是 v3；
- LRU 只可删除 W09 本地 cache，不得触碰 S3。

## 9. IAM 最小合同

此处是待审计的权限合同，不是可直接执行的 policy。

### 9.1 W09 允许

- `s3:ListBucket`：仅 `research/releases/`；
- `s3:GetObject` / `s3:GetObjectVersion`：仅
  `research/releases/*/MANIFEST.json`；
- `s3:GetObjectVersion`：仅固定 canonical warehouse/control allowlist，
  且目标精确版本满足 `s3:ExistingObjectTag/research-eligible=true`。
- `s3:GetObjectVersion`：Resource 仅匹配
  `ec2/raw/date=*/rfq_*.ndjson*` 与
  `ec2/raw/date=*/rfq_receipts_*.ndjson*`，且目标精确版本同时满足
  `research-eligible=true` 和 `research-channel=rfq`；不授予其他 raw family。

canonical 数据不授予 `s3:GetObject`，使不带 VersionId 的当前版本读取失败；
不授予 canonical 的 List/ListVersions。

### 9.2 W09 显式禁止

- `s3:GetObjectVersion` on 所有非 RFQ raw、active/unsealed/blocked RFQ；
- versionless `s3:GetObject` on 全部 `ec2/raw/*`，包括合格 RFQ；
- versionless `s3:GetObject` on canonical 数据前缀；
- 所有 S3 Put/Delete/Tagging/Restore/ACL/Multipart/Lifecycle 动作；
- 生产 EC2 control-plane；
- 静态 AWS access key。

IAM 无法动态读取 manifest 并只放行“本 manifest 点名的 VersionId”。因此
安全边界是三层，而不是虚假声称 IAM 单独完成：

```text
IAM：只能读 allowlisted + research-eligible 的精确版本；RFQ 再加 channel tag
consumer：只能请求当前 manifest 明列的精确版本
SHA verifier：下载后证明字节与 manifest 完全一致
```

### 9.3 producer 与 tagger 权限

- manifest publisher 只获得读取 verified receipts、读取 exact source
  versions 和 conditional-create `MANIFEST.json` 所需权限；
- 唯一 dedicated eligibility tagger 只能在 operator 单独批准后获得限定
  warehouse/control 与 RFQ resource patterns 的
  `GetObjectVersionTagging` / `PutObjectVersionTagging`；
- dedicated tagger 不获得 `PutObjectTagging`，强制每次写入带 exact VersionId；
- IAM/bucket policy 必须移除或拒绝其他 automation principals 在相关资源上的
  `PutObjectTagging` 和 `PutObjectVersionTagging`。S3 管理员权限不假装能够被
  本计划消灭，因此由 drift patrol 监测；
- tagger 必须保留 preflight 读到的已有 tags，并在写后回读验证；
- W09 永远没有任何 tag 读写权限。

## 10. 执行拆分和逐步停止门

本总计划通过独立审计并由 operator ratify 后，仍不得一次性全做。必须拆成
三个独立 session/W；每个 W 都有自己的 diff、测试、审计和退出仪式。

### W-PUB-REF-01A — Receipts 与 prune shadow

允许：

- 补齐 canonical sync 对 seal、facts、dim/catalog、corrections、gap/L2
  quality/control 对象的覆盖；
- 生成 exact-version receipts 和本地 receipt index；
- 给 warehouse/control 和合格 sealed RFQ 的精确版本加 eligibility tag；
- receipt gate 先 shadow，再 dry-run。

阶段门：

1. shadow 只产 receipts，不改 prune、不写 tags；
2. 对已封存日回填并做 desired-set/receipt-set 双向相等检查；
3. 单独审计并由 operator 应用 dedicated single-writer tagger policy 后，才给
   合格 exact versions 打 tag；
4. prune dry-run 对比旧候选；
5. 独立审计 PASS 后才能启用 receipt gate；
6. 缺 receipt 一律保留并生成 durable yellow alert artifact。

shadow receipt 保持不可变；tag phase 改变 eligibility state 时创建新的 digest
receipt 并复用原 byte attestation，不覆盖 shadow receipt。

本 W 不新增 Telegram 集成。若现有通用告警通道能无代码复用，可由 operator
另行决定转发；否则磁盘/S3 控制证据就是权威告警。

### W-PUB-REF-01B — v3 publisher 与 dual consumer

允许：

- 新 v3 schema、deterministic publisher 和 conditional manifest write；
- v2/v3 schema dispatch；
- exact-version downloader、content-addressed cache、atomic verified view；
- `--include-rfq` / `--with-rfq` 的 sealed-only reference fixtures 和负向夹具；
- 离线夹具、单元测试和本地负向测试。

默认值必须保持 v2；本 W 不改 IAM、不接生产 W09、不切默认 publisher。

### W-PUB-REF-01C — W09 read IAM、canary、rollback、cutover

允许：

- operator 审核并应用最小 W09 read IAM/bucket-policy 变更；
- 选择一个已有 VERIFIED v2 的 sealed day 手工生成旁路 v3；
- 在 W09 上手工 fetch、verify、build view、运行固定研究 smoke；
- 执行 rollback drill；
- 证据 PASS 后，另经 operator 明示裁决切换新 release 默认 publisher。

canary 分两条：

1. core canary 必须选择已有 VERIFIED v2 且同时覆盖 L1、L2、盘口和质量
   证据的封存日；
2. RFQ canary 只可选择已有 `SEALED_CONFIRMATION`、receipt PASS、非 blocked/
   quarantine 的对象，并显式使用 `--include-rfq` / `--with-rfq`。若当前没有
   合格生产对象，core v3 可继续验收，但 RFQ publication flag 必须保持 OFF，
   直到单独的 RFQ canary PASS；不得为制造 canary 去 repair 数据。

`2026-07-13` 仅是 core 候选，执行时必须先由权威 seal status table 和 v2
manifest 确认，不得沿用聊天记忆。

## 11. 验收矩阵

| 类别 | 必须 PASS 的证据 |
|---|---|
| Capture 隔离 | 实施窗口前后 heartbeat/recorder/gap 证据无本变更造成的回归；sync/publish 失败不影响 capture/ingest/seal |
| Receipt 完整性 | desired object set 与 receipt set 双向 100% 相等；每项 VersionId 非空且 exact GET 的 size/SHA PASS |
| Prune 安全 | receipt 缺失、损坏、null VersionId、hash mismatch、journal 中断、跨日依赖夹具均为零误删 |
| v3 数据面 | `research/releases/<v3>/` 只含一个 MANIFEST；canonical receipt 单独留在 control plane；重复发布大数据上传 0 bytes |
| v2/v3 等价 | 在相同 `rfq_included` 口径下，同日 logical object set、size、SHA、schema、row count 完全一致；固定查询的规范化结果 hash 一致 |
| Consumer | inventory 标明模式；v2 旧流程仍 PASS；v3 cache 命中复核 SHA；损坏对象 fail closed/refetch |
| IAM 正向 | W09 可读取 manifest 指定、tagged、allowlisted 的 warehouse/control exact VersionId；RFQ 仅双 tag exact version 成功 |
| IAM 负向 | 非 RFQ raw、active/unsealed/blocked RFQ、未 tagged version、canonical List、versionless canonical GET、Put/Delete/Tag/Restore 全部失败 |
| RFQ opt-in | 默认 fetch 不下载 RFQ 且如实标 `NOT_FETCHED_OPT_IN`；合格 canary 的 `--with-rfq` 才可得到 `VERIFIED_SEALED_RAW`；无任何 repair |
| Manifest 负向 | 错 bucket/prefix/date/size/SHA/VersionId、重复 logical key、path traversal、overwrite race 全部 fail closed |
| Rollback | 关闭 v3 默认后可立即选最后一个 VERIFIED v2，且无 S3 删除、重写或 canonical 变化 |
| 本地去重 | 两个 release 引用同 SHA 时 cache 仅一份物理内容，清理只影响 W09 本地磁盘 |

报告文件可能包含运行时间和本地路径，因此不要求报告文件 byte-identical；
要求输入集合和规范化核心研究结果相同。

## 12. Patrol、retention 与告警

本 W 不修改 lifecycle。canary 前必须证明现有 lifecycle 不会 expire、删除或
转入需 Restore 的存储层的 referenced VersionIds。没有该证据就不切 v3 默认。

巡检分两级：

- 发布和 W09 fetch：完整 exact-version size/SHA 验证；
- 日常 patrol：metadata-only 检查 exact VersionId 存在、size、tag、storage
  class 和 policy drift，避免每天重读全部大数据。

日常 patrol 在 production-side inspector/audit role 上运行；它可读取版本
metadata/tags 和写小型告警证据，但没有数据 Put/Delete 权限。W09 不因巡检
获得任何 Tagging、List canonical 或 IAM inspection 权限。

patrol 失败：冻结新 v3 发布、保留最后 VERIFIED release、写 durable yellow
alert artifact。不得自动改 lifecycle、删对象或扩权。

tag 和 IAM 不能阻止 S3 Lifecycle 或管理员删除。若未来要求抵御这两类操作的
绝对不可删除保证，必须另立计划评估 Object Lock/Legal Hold；本计划不得冒充
已经提供这种保证。

## 13. Rollback

触发条件包括：

- exact-version GET 或 integrity patrol 失败；
- IAM 越界或任一禁止动作意外成功；
- v2/v3 输入或核心结果不一致；
- manifest overwrite/equivocation；
- capture continuity 出现与变更相关的异常；
- lifecycle 无法证明不破坏引用版本。

动作固定为：

1. 停止新的 v3 publication/cutover；
2. W09 选择最后一个 VERIFIED `COPIED_V2` release；
3. prune 切到“停止删除、继续保留”，不能退回 seal-only prune；
4. 保留所有 v3 manifest、receipts 和失败证据供审计；
5. 不删除、不覆盖、不改 canonical 对象；
6. 新问题另立 W，不在事故中临时扩权或修数据。

## 14. 明确的 operator 动作

当前只需要 operator 做一件事：把本计划交给独立 agent 审计。

未来只有在审计 PASS 后，operator 才分别决定是否：

1. ratify 本总计划；
2. 发出 W-PUB-REF-01A 执行令；
3. 在 01A/01B 都 PASS 后批准 IAM canary；
4. 在 canary 和 rollback drill PASS 后批准切换默认 publisher。

任何 agent 都不得把“计划已写好”解释成上述批准已发生。

## 15. 独立审计入口

请独立审计 agent 不执行实现，只回答：

1. v3 是否真正做到 S3 大数据零复制，同时保持可复现？
2. IAM、publisher 和 consumer 是否只放行 sealed/receipt-verified 的 RFQ
   exact VersionId，同时拒绝所有其他 raw 与 blocked/quarantined RFQ？
3. receipts 是否足以安全绑定 VersionId/size/SHA，并安全门控 local prune？
4. canonical 所需的 gap、L2 quality、corrections、dim/catalog 是否有漏项？
5. IAM 是否只允许 exact-version read，且没有把“manifest 点名限制”错误归功
   于 IAM？
6. v2 兼容、canary、rollback 和 lifecycle 风险是否可操作？
7. W-PUB-REF-01A/B/C 的授权边界是否足够小，能一 W 一审计？
8. 本计划是否保留 RFQ 安全读取能力、同时严格遵守“禁止任何 RFQ repair”、
   人工 W09 启停和“禁止新增 Telegram 功能”？

审计输出必须给出 `PASS`、`PASS_WITH_RIDERS` 或 `FAIL`，逐条列出 blocking
finding、证据路径和最小修订，不得顺手改代码、AWS 或生产状态。

## 16. 本地依据

- `docs/PLAN_NEXT_PHASE_DATA_QUEUE_EN_2026-07-16.md`
- `docs/PLANS_LEDGER.md`
- `deploy/w09/README.md`
- `deploy/w09/research_data_instance_profile.py`
- `sandbox/w05-recovery-fix/tools/research_release.py`
- `sandbox/w05-recovery-fix/tools/research_data.py`
- `sandbox/w05-recovery-fix/deploy/ec2_s3_sync.sh`
- `sandbox/w05-recovery-fix/tools/prune_raw.py`
- `sandbox/w05-recovery-fix/docs/plan_releases/pipeline/PIPE-W05-SPEC-2026-07-12.md`
- `sandbox/wa-dev/docs/plan_releases/pipeline/W05_RESEARCH_READONLY_IAM_POLICY.json`

## 17. AWS 语义依据

- S3 exact VersionId retrieval:  
  <https://docs.aws.amazon.com/AmazonS3/latest/userguide/RetrievingObjectVersions.html>
- S3 checksum verification:  
  <https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html>
- Tag-based S3 access conditions:  
  <https://docs.aws.amazon.com/AmazonS3/latest/userguide/tagging-and-policies.html>
- Conditional writes:  
  <https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html>
- S3 Object Lock is a separate operational decision:  
  <https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html>
