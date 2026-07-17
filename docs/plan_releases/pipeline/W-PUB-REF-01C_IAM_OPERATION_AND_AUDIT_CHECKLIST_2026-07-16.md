# W-PUB-REF-01C IAM 操作与审计清单

状态：**DRAFT / NOT APPLIED / AUDIT ARTIFACTS ONLY / RFQ AWS HARD OFF**。

这里的四份 JSON 只是待审计策略，不会被任何脚本自动应用。本次变更没有调用 AWS 写 API，也没有修改现有 IAM、bucket policy、实例角色或 S3 对象。

RFQ 当前仍是 **DATA_INTEGRITY_BLOCKED / OFF**。当前 base IAM 包没有 RFQ Allow；W09 与 tagger 还有 raw namespace explicit Deny，bucket fragment 对 RFQ 版本标签写删不保留任何例外。未来接口只存档在 `W-PUB-REF-01C_FUTURE_RFQ_DELTA_NOT_ATTACHED.md`，不是启用令，不启动 repair，不允许补数据、重跑、加标签或读取 RFQ。

## 四份策略分别做什么

1. `W-PUB-REF-01C_W09_IDENTITY_POLICY.json`
   - W09 只能列出 `research/releases/`，只能按当前版本读取 `research/releases/*/MANIFEST.json`。
   - canonical 非 RFQ 对象只授予 `s3:GetObjectVersion`，且对象版本必须有 `research-eligible=true`。
   - `ec2/warehouse/*`、`ec2/control/*` 的 versionless `GetObject` 被显式拒绝；缺 eligibility tag 的 exact-version read 也被显式拒绝，不能被同 role 的其他 Allow 绕过。
   - 整个 `ec2/raw/*` 的 `GetObject`、`GetObjectVersion` 和 tag read 都被显式拒绝；当前 W09 无 RFQ 读取能力。
   - 不授予 canonical `ListBucket`、`GetObject` 或任何 latest/versionless data read；显式拒绝对象写入、删除、改标签和恢复。
2. `W-PUB-REF-01C_PUBLISHER_TAG_INSPECTION_DELTA.json`
   - 对 canonical allowlist 增加 `s3:GetObjectVersionTagging`，供 publisher 按 manifest/receipt 中的精确 `VersionId` 复核标签。
   - 只对 `research/releases/*/MANIFEST.json` 前缀增加 `s3:ListBucketVersions`，用于证明同名 manifest 从未出现第二版本或 delete marker。
   - 不增加任何 raw/RFQ 权限，也不增加任何 tag write。
   - 没有 `PutObjectTagging` 或 `PutObjectVersionTagging`。
3. `W-PUB-REF-01C_TAGGER_IDENTITY_POLICY.json`
   - 专用 tagger 只可读取/写回 allowlist 内精确版本的标签；canonical 非 RFQ 写标签请求必须包含 `research-eligible=true`。
   - 整个 `ec2/raw/*` 的 tag read/write/delete 被显式拒绝；当前 tagger 无 RFQ 标签能力。
   - tagger 只可在 canonical receipt 路径执行带 `If-None-Match: *` 的 `PutObject`，并仅可列该精确 receipt key 的版本历史、按 `VersionId` 读回。
   - 大型 `warehouse/corrections/date=D/late_rows.ndjson` 只绑定原 canonical 对象的精确 `VersionId`；不得复制到 publication snapshot，也不得进入 small-controls 上传。只有小型 date-only ledger projection 走 content-addressed control 路径。
   - 显式拒绝 versionless `s3:PutObjectTagging`。
4. `W-PUB-REF-01C_BUCKET_POLICY_MERGE_FRAGMENT.json`
   - 这不是完整 bucket policy。它只有 `Deny` 语句，必须合并进现有 policy 的 `Statement` 数组。
   - 精确版本的标签写/删只放行 `TAGGER_ARN`；versionless 标签写/删对所有 principal（包括 tagger）都拒绝。
   - 两个精确 RFQ object glob 的 versioned tag write/delete 对所有 principal 无条件拒绝；当前 tagger 也没有例外。fragment 不授予任何权限。
   - `research/releases/*/MANIFEST.json` 对所有 principal 禁止删除；缺 `If-None-Match: *` 的 Put 也拒绝。publisher 仍需在写前、no-op 和写后检查 exact-key version history。
   - canonical receipt 与 raw-prune-authority receipt 同样禁止删除，并拒绝不带 `If-None-Match: *` 的 Put；local prune 只在 authority exact readback 后读取本地原子 index。

## RFQ key 名称边界

仓库实际名称由 `canonical_receipts.py` 的正则约束为 `rfq_<两位数字>.ndjson[.<数字 shard>]` 或 `rfq_receipts_<两位数字>.ndjson[.<数字 shard>]`。IAM ARN wildcard 只有 `*`/`?`，不能表达“必须是数字”或“后缀不能含 `/`”，所以策略先把日期长度/连字符和 basename 收紧为：

- `ec2/raw/date=????-??-??/rfq_??.ndjson*`
- `ec2/raw/date=????-??-??/rfq_receipts_??.ndjson*`

这仍不是正则，也不是名称真实性证明；因此这些 glob 当前只用于无条件 Deny。未来若另案授权，仍必须由 sealed receipt exact set 和应用正则再次拒绝非数字日期/小时、非数字 shard、额外 `/` 或未被 seal 点名的 key。

## 上线前必须做，顺序不可交换

1. 创建专用 pathless IAM user `canonical-eligibility-tagger`，并核对实际 ARN 必须精确等于 `arn:aws:iam::321572485933:user/canonical-eligibility-tagger`；同时记录 `UserId`。不要用 role、带 IAM path 的 user 或 STS `assumed-role/.../session` ARN。源 fragment 含 `TAGGER_ARN`，**绝不能原样应用**；原样应用会把所有真实 principal 锁死。只允许使用仓库渲染/校验工具生成且确认已无占位符的版本，再进入合并步骤。
2. 导出现有 bucket policy 原文并记录 SHA-256。确认它包含的复制、生命周期、日志、KMS 或其他生产规则不会被新 Deny 意外破坏。
3. 只把 fragment 的 `Statement` 合并到现有 policy。**禁止把 fragment 单独执行 `PutBucketPolicy`，否则会覆盖现有 policy。** 合并后再次保存全文及 SHA-256，并检查 policy 大小限制。
4. tagger identity policy 去空白后约 5,354 字符，超过 IAM user inline policy 的 2,048 字符总限额；必须先创建 customer-managed policy，再只附加到新建的专用 tagger user。核对该 managed policy 的全部 attachments、permissions boundary、SCP、access point policy 与 bucket policy；不得附加给 publisher、W09 或其他 automation。
5. 把 publisher delta 合并/附加到 publisher role。检查所有 inline/managed policy，确认 publisher 没有任何 object-tag 写权限；bucket policy 的显式 Deny 是第二层保护。
6. W09 policy 先在 canary role 上测试。原有 `research/*` broad read policy 若继续存在，会使“只读 manifest”边界失效；必须在 v3 canary 通过并明确切换时再移除/替换，不能静默叠加后声称已收紧。
7. 如果对象使用 SSE-KMS，另行确认精确 KMS key 及最小 `kms:Decrypt`/写入所需权限。本包没有猜测或授予任何 KMS 权限。
8. 用 IAM Access Analyzer `ValidatePolicy` 检查四份替换占位符后的完整策略；再用目标 user 的真实凭证做正反测试。JSON 可解析不等于 IAM/bucket policy 已安全上线。
9. RFQ 保持 AWS HARD OFF：四份 base policy 不得添加 RFQ Allow，不得附加 future delta，不得使用 RFQ include/enable 参数，不得创建 RFQ eligibility evidence，不得把旧 repair 收据解释为授权。
10. canary 与 rollback drill PASS 前，**不得**创建 `~/.kalshi/research_zero_copy_v3_cutover_approved`。此时 post-seal 仍发布 copied-v2 作为回滚路径，但命令行强制 `--no-rfq`。只有 operator 审阅 canary 证据并明确批准 cutover 后才创建该文件；创建后旧入口只记 `LEGACY_RESEARCH_COPY_DISABLED`，不再上传大对象。
11. canary 后立即用 `tools/research_reference_patrol.py` 对已下载 v3 MANIFEST 执行 exact-version HEAD/tag patrol，并提供 24 小时内、digest 绑定的真实 policy evidence。alert 路径必须是主生产 live root 下的 `research_reference_patrol/YELLOW.json`；detached code worktree 不得另起一份。任何 YELLOW 都冻结新 v3 publish，后续 PASS 也不会自动清除，必须人工审阅。
12. `publish-reference --live-dir` 必须显式指向主生产 `/home/ubuntu/hft-bot/work/live`。真实 S3 v3 发布不能用 CLI 或环境变量改写 YELLOW 路径；publisher 在入口和最终 PutObject 前各检查一次。canary/cutover 后才安装 daily timer，且 timer 的失败不得阻塞 capture、ingest 或 seal。

## 必须通过的正反测试

- W09：能 list research release namespace、能读 MANIFEST；不能读 release 内复制的数据对象。
- W09：不能 list `ec2/warehouse`、`ec2/control`、`ec2/raw`；不能 versionless `GetObject` canonical 数据；任何 raw/RFQ GET、exact GET 或 tag GET 都被拒绝。
- W09：带 receipt 指定 VersionId 且有 eligibility tag 的 warehouse/control 对象可读；无 tag、tag=false、错误 VersionId、非 allowlist 路径都拒绝。
- W09：Put、Delete、Put/Delete Tagging、Restore 全部拒绝。
- publisher delta：只为 canonical 新增精确 `GetObjectVersionTagging`，并只列 manifest version history；文件中不得出现 `ec2/raw`，任何 tag write 都不得授予。
- tagger：`GetCallerIdentity` 必须与审计记录的 IAM user ARN 一致；普通 canonical 精确版本 tag preflight/readback 可用；任何 raw/RFQ tag read/write/delete 都被拒绝。
- tagger：不带 `versionId` 的 PutObjectTagging 拒绝；其他 principal 的 versioned tag write/delete 被 bucket policy 拒绝。
- receipt：首次 `If-None-Match: *` conditional create 成功并返回非 null VersionId；同 key 再写不能产生第二版本；ListObjectVersions 只能看到一个版本且没有 delete marker；随后 exact HEAD/GET 字节验证通过。
- raw prune authority：只列 receipt 已完整 SHA 验证的同日非 RFQ raw exact versions；RFQ 与跨日对象逐对象 deferred/retain。远端小 receipt conditional-create + exact readback 成功以前，不得写本地 `PRUNE_AUTHORITY.json`。
- manifest：首次 conditional create 前 history 必须为空；写后 exact key 只能有刚返回的一个 VersionId 且没有 delete marker；历史版本、delete marker、截断 history、并发 create 和任何 delete 都必须 fail closed。

## single-writer 审计收据

只有在上述测试全部通过后，才可生成 `canonical-eligibility-single-writer-audit-v1`。证据包至少应保存：

- 完整现有/合并后 bucket policy、tagger/publisher/W09 的 inline 与 managed policy 版本、managed-policy attachments、permissions boundary、相关 SCP；tagger 是 IAM user，因此没有 role trust policy；
- 三个实际 `sts:GetCallerIdentity` 输出；
- 上述正反测试的命令、UTC 时间、request ID、VersionId 与结果；
- policy evidence 文件的字节数和 SHA-256。

在这些证据存在以前，`exclusive_exact_version_writer`、`versionless_tagging_denied`、`other_automation_tag_writers_denied` 三项不得写成 `true`，也不得运行 eligibility tagger 的批准参数。

上述 single-writer 收据只证明“谁能写标签”，不等于 RFQ 数据获准。RFQ 还需要独立 sealed-evidence 授权；当前没有该授权，因此 RFQ 必须继续 BLOCKED/OFF，并保持 no-repair。

## AWS 官方语义依据

- S3 API 与 IAM action 对照（带 `versionId` 的 GET 使用 `s3:GetObjectVersion`；ListObjectVersions 使用 `s3:ListBucketVersions`；版本化标签 API 使用对应 VersionTagging action）：https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html
- `s3:ExistingObjectTag/<tag-key>` 与 `s3:RequestObjectTag/<tag-key>` 的对象标签访问控制：https://docs.aws.amazon.com/AmazonS3/latest/userguide/tagging-and-policies.html
- `If-None-Match: *` conditional write 与所需 `s3:PutObject` 权限：https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- 用 `s3:if-none-match` 在策略中强制 conditional write：https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html
- AWS 推荐在 resource policy 的 Deny 中使用 `ArnNotEquals` + `aws:PrincipalArn`，以及 role session 的 PrincipalArn 取值：https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html
- IAM 显式 Deny 的评估逻辑：https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html
