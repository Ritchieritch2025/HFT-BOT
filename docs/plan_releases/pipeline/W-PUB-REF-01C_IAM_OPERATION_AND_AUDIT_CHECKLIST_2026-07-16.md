# W-PUB-REF-01C IAM 操作与审计清单

状态：**CONTROLLED RUNBOOK / LIVE READBACK REQUIRED / RFQ AWS HARD OFF**。

这里的五份 JSON 是安全边界输入，不会被仓库脚本自动应用。tagger identity 与完整 bucket target 的 operator-attested delivery 另见 `W-PUB-REF-01C_TAGGER_POLICY_DELIVERY_2026-07-17.md`；该证明不能替代 broker、publisher、W09 和当前 bucket policy 的 authenticated live readback。本次文档变更没有调用 AWS 写 API，也没有修改现有 IAM、bucket policy、实例角色、systemd 或 S3 对象。

RFQ 当前仍是 **DATA_INTEGRITY_BLOCKED / OFF**。当前 base IAM 包没有 RFQ Allow；W09 与 tagger 还有 raw namespace explicit Deny，bucket fragment 对 RFQ 版本标签写删不保留任何例外。未来接口只存档在 `W-PUB-REF-01C_FUTURE_RFQ_DELTA_NOT_ATTACHED.md`，不是启用令，不启动 repair，不允许补数据、重跑、加标签或读取 RFQ。

## 五份策略分别做什么

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
4. `W-PUB-REF-01C_CREDENTIAL_BROKER_IDENTITY_POLICY.json`
   - 专用 pathless IAM user `canonical-credential-broker` 只可对精确资源 `arn:aws:iam::321572485933:user/canonical-eligibility-tagger` 调用 `iam:GetUser`、`iam:ListAccessKeys`、`iam:CreateAccessKey`、`iam:UpdateAccessKey`、`iam:DeleteAccessKey`。
   - 不允许管理任何其他 user，不授予 S3、publisher、W09、RFQ、policy attach/update 或 user create/delete 权限；不得把该 policy 附加给 `vaultWriter`、tagger 或其他 automation。
   - broker 的 authenticated ARN 与 immutable `UserId` 必须与六字段 identity evidence 一致；tagger key 创建前必须先观察到零 key，完成后必须失活、删除并连续两次观察到零 key。
5. `W-PUB-REF-01C_BUCKET_POLICY_MERGE_FRAGMENT.json`
   - 这不是完整 bucket policy。它只有 `Deny` 语句，必须合并进现有 policy 的 `Statement` 数组。
   - 精确版本的标签写/删只放行 `TAGGER_ARN`；versionless 标签写/删对所有 principal（包括 tagger）都拒绝。
   - 两个精确 RFQ object glob 的 versioned tag write/delete 对所有 principal 无条件拒绝；当前 tagger 也没有例外。fragment 不授予任何权限。
   - `research/releases/*/MANIFEST.json` 对所有 principal 禁止删除；缺 `If-None-Match: *` 的 Put 也拒绝。publisher 仍需在写前、no-op 和写后检查 exact-key version history。
   - canonical receipt 与 raw-prune-authority receipt 同样禁止删除，并拒绝不带 `If-None-Match: *` 的 Put；local prune 只在 authority exact readback 后读取本地原子 index。

`W-PUB-REF-01C_BUCKET_POLICY_FULL_TARGET_2026-07-17.json` 是当时 operator-attested 空 baseline 上渲染出的完整 target，不是第六份独立授权。只要 live bucket policy 不再为空，就必须重新从 live 原文合并、校验和 readback，绝不能覆盖。

## 当前凭据模型：broker 可常驻，tagger 必须零常驻

- `vaultWriter` 只进入 publisher 子进程环境；它不能调用 IAM access-key lifecycle API。旧日志里由 `vaultWriter` bootstrap tagger 的说法已由 `docs/SESSION_LOG.md` 顶部勘误废止。
- `canonical-credential-broker` 是独立的长期身份，其加密凭据只供 full-daily systemd unit 使用。它只创建/清理 tagger 临时 key，不获得 S3 数据面权限。
- `canonical-eligibility-tagger` 必须在每次 transaction 开始和结束时都是零 access key。bootstrap 先核对 broker STS identity、tagger `GetUser` ARN/UserId 和零 key，再创建一个内存态 key；临时 key 只传给受限 tagger 子进程。
- 正常退出、tagger 失败、SIGINT/SIGTERM 或不确定 create response 都必须进入 cleanup：对发现的 key 先 `Inactive`、再 `Delete`，至少经过 2 秒 consistency window 并连续两次 `ListAccessKeys=[]` 才返回。cleanup 失败必须让整轮失败，不得发布成功收据。
- access key ID 与 secret 不得写入仓库、文档、identity evidence、普通文件、命令行、shell history、journal 或业务收据；Python memory 清理只是 best effort，安全终点仍是 IAM 删除与 double-zero readback。

## 六字段 identity evidence

固定路径是 `/etc/kalshi-research-v3/ephemeral-tagger-identities.json`。根对象必须**恰好**包含以下六项，不能加注释或额外字段：

| 字段 | 固定/证据值 |
|---|---|
| `schema_version` | `canonical-ephemeral-tagger-identities-v2` |
| `account` | `321572485933` |
| `broker_arn` | `arn:aws:iam::321572485933:user/canonical-credential-broker` |
| `broker_user_id` | authenticated `iam:GetUser` 返回的 immutable `UserId` |
| `tagger_arn` | `arn:aws:iam::321572485933:user/canonical-eligibility-tagger` |
| `tagger_user_id` | authenticated `iam:GetUser` 返回的 immutable `UserId` |

文件必须是单 hard-link、非 symlink、root:root、`0400` 或 `0440`、非空且不超过 16 KiB；父目录必须 root-owned 且 group/world 不可写。两个 `UserId` 必须匹配 `[A-Z0-9]{16,128}`，不能用 ARN、用户名、AccessKeyId 或人工猜值替代。该文件不是密钥，但它是防止同名 IAM user 被删除重建后静默接管的 fail-closed pin。

## systemd encrypted credential 边界

- 固定 broker blob 是 `/etc/credstore.encrypted/kalshi-research-v3-credential-broker.env`；目录必须 root:root `0700`，blob 必须是非 symlink、root:root `0600`、非空且不超过 1 MiB。只能通过批准的 secret-injection 流程和 `systemd-creds` 生成；不得先把明文落到普通临时文件，也不得在命令参数或 shell history 中出现密钥。
- 解密 payload 只允许简单 AWS assignment；必须有 `AWS_ACCESS_KEY_ID` 与 `AWS_SECRET_ACCESS_KEY`，可选 `AWS_SESSION_TOKEN/AWS_REGION/AWS_DEFAULT_REGION`。任何其他 `AWS_*`、命令替换、反引号或扩展语法都会 fail closed。
- full-daily unit 通过 `LoadCredentialEncrypted=credential-broker.env:...` 把解密内容放在 systemd credential mount，再以 `%d/credential-broker.env` 传给 coordinator；程序拒绝任意其他 production path。不要手工 decrypt 来“验证内容”，只校验文件 metadata，并通过受控 unit 的 STS/identity gate 验证。
- publisher blob `/etc/credstore.encrypted/kalshi-research-v3-publisher.env` 必须保持独立文件、独立 access-key identity；程序会拒绝 broker/publisher 指向同一文件或同一 AccessKeyId。
- 旧 standing tagger blob `/etc/credstore.encrypted/kalshi-research-v3-tagger.credentials` 必须不存在。service 中不得出现 `LoadCredentialEncrypted=tagger.credentials`。
- full-daily 必须运行在独立 UID `kalshi-research-v3-credential-broker`，并保持 `ProtectProc=invisible`、`ProcSubset=pid`、`NoNewPrivileges=true`、空 capability bounding set、`PrivateTmp=true`。共享 `ephemeral-tagger.lock` 必须是该 UID 可写的单一 `0600` regular file。

## RFQ key 名称边界

仓库实际名称由 `canonical_receipts.py` 的正则约束为 `rfq_<两位数字>.ndjson[.<数字 shard>]` 或 `rfq_receipts_<两位数字>.ndjson[.<数字 shard>]`。IAM ARN wildcard 只有 `*`/`?`，不能表达“必须是数字”或“后缀不能含 `/`”，所以策略先把日期长度/连字符和 basename 收紧为：

- `ec2/raw/date=????-??-??/rfq_??.ndjson*`
- `ec2/raw/date=????-??-??/rfq_receipts_??.ndjson*`

这仍不是正则，也不是名称真实性证明；因此这些 glob 当前只用于无条件 Deny。未来若另案授权，仍必须由 sealed receipt exact set 和应用正则再次拒绝非数字日期/小时、非数字 shard、额外 `/` 或未被 seal 点名的 key。

## 上线前必须做，顺序不可交换

1. 创建或 readback 两个专用 pathless IAM user：`canonical-eligibility-tagger` 与 `canonical-credential-broker`。实际 ARN 必须分别精确等于上述固定 ARN，并从 authenticated `iam:GetUser` 记录各自 immutable `UserId`。不要用 role、带 IAM path 的 user 或 STS `assumed-role/.../session` ARN。tagger 必须先清到零 access key；broker policy 只可附加给 broker。源 bucket fragment 含 `TAGGER_ARN`，**绝不能原样应用**；只允许使用仓库渲染/校验工具生成且确认已无占位符的版本，再进入合并步骤。
2. 导出现有 bucket policy 原文并记录 SHA-256。确认它包含的复制、生命周期、日志、KMS 或其他生产规则不会被新 Deny 意外破坏。
3. 只把 fragment 的 `Statement` 合并到现有 policy。**禁止把 fragment 单独执行 `PutBucketPolicy`，否则会覆盖现有 policy。** 合并后再次保存全文及 SHA-256，并检查 policy 大小限制。
4. tagger identity policy 去空白后约 5,354 字符，超过 IAM user inline policy 的 2,048 字符总限额；必须先创建 customer-managed policy，再只附加到新建的专用 tagger user。核对该 managed policy 的全部 attachments、permissions boundary、SCP、access point policy 与 bucket policy；不得附加给 publisher、W09 或其他 automation。
5. 把 publisher delta 合并/附加到实际 publisher principal（当前 readback 是 IAM user `vaultWriter`）。检查所有 inline/managed policy，确认 publisher 没有任何 object-tag 写权限或 IAM key-lifecycle 权限；bucket policy 的显式 Deny 是第二层保护。
6. W09 policy 先在 canary role 上测试。原有 `research/*` broad read policy 若继续存在，会使“只读 manifest”边界失效；必须在 v3 canary 通过并明确切换时再移除/替换，不能静默叠加后声称已收紧。
7. 如果对象使用 SSE-KMS，另行确认精确 KMS key 及最小 `kms:Decrypt`/写入所需权限。本包没有猜测或授予任何 KMS 权限。
8. 用 IAM Access Analyzer `ValidatePolicy` 检查五份替换占位符后的完整策略；再用目标 principal 的真实身份做正反测试。JSON 可解析不等于 IAM/bucket policy 已安全上线。
9. RFQ 保持 AWS HARD OFF：五份 base policy 不得添加 RFQ Allow，不得附加 future delta，不得使用 RFQ include/enable 参数，不得创建 RFQ eligibility evidence，不得把旧 repair 收据解释为授权。
10. canary 与 rollback drill PASS 前，**不得**创建固定的 `/etc/kalshi-research-v3/approvals/cutover-approved` 与 `/etc/kalshi-research-v3/approvals/publish-approved` 两个 arm；旧 home-directory cutover marker 不是当前 full-daily 授权。只有 operator 审阅 canary 证据并明确批准后才同时建立当前两个 arm。rollback 只冻结后续 v3 publication，不以恢复 standing tagger 或 copied-v2 作为回退。
11. canary 后立即用 `tools/research_reference_patrol.py` 对已下载 v3 MANIFEST 执行 exact-version HEAD/tag patrol，并提供 24 小时内、digest 绑定的真实 policy evidence。alert 路径必须是主生产 live root 下的 `research_reference_patrol/YELLOW.json`；detached code worktree 不得另起一份。任何 YELLOW 都冻结新 v3 publish，后续 PASS 也不会自动清除，必须人工审阅。
12. `publish-reference --live-dir` 必须显式指向主生产 `/home/ubuntu/hft-bot/work/live`。真实 S3 v3 发布不能用 CLI 或环境变量改写 YELLOW 路径；publisher 在入口和最终 PutObject 前各检查一次。canary/cutover 后才安装 daily timer，且 timer 的失败不得阻塞 capture、ingest 或 seal。

## 安全部署与启用

1. 在 AWS 侧完成五份 policy 的 ValidatePolicy、attach/merge 和 authenticated readback；保存 policy bytes/SHA、attachments、permissions boundary/SCP 与两个 IAM `UserId`。确认 tagger `ListAccessKeys` 为空，再为 broker 创建其独立凭据；不要为 tagger 创建 standing key。
2. 用批准的 secret-injection 流程生成 broker systemd encrypted blob，并安装上述六字段 evidence。只检查 owner/mode/link-count/size/hash；任何终端、日志和交接文件都不得显示解密内容。确认 publisher blob 独立且 legacy tagger blob 不存在。
3. 只从完全 clean、已审计的 commit 运行 `sudo deploy/install_kalshi_research_v3_daily.sh`。installer 会先停止/禁用旧 full-daily writer，再验证凭据形状、identity schema、固定 AWS CLI/OS Python、immutable runtime、locks 和 systemd sandbox。失败时旧 writer 保持停止；不要手工绕过拒绝项。
4. installer 成功必须报告 `mode=ephemeral-full-publication`；若只报告 `mode=publisher-only-durable`，表示 broker blob 或 identity evidence 至少一项缺失，full publication 仍安全关闭。installer 只 enable triggers，不主动 start。
5. readback systemd：witness 的 `OnSuccess` 必须恰好是 durable；仅在 full mode 时 durable 的 `OnSuccess` 才能是 daily，且 daily timer 才能 enabled。full-daily unit 必须只加载 publisher/broker 两份 encrypted credential 加一份六字段 evidence，绝不能加载 tagger credential。capture/ingest/seal unit 不得被停止、重启或添加依赖。
6. 在创建两个固定 approval arm 以前，完成 broker/tagger 正反 canary、double-zero cleanup 和 W09 canary policy 验证。明确批准后再创建 arms，手动启动一次 full-daily canary；等待 `ExecMainStatus=0/Result=success`、tagger 零 key、tag/readback receipt、write-once manifest 和 patrol PASS，随后才让 30 分钟 retry timer 接管。
7. 发布成功必须逐日绑定 sealed durable receipt、exact VersionId tags、MANIFEST VersionId/SHA 和 Research exact query receipt；任何日期失败只留失败收据并重试，不能影响 capture，也不能把未合格日期或 RFQ 推进研究目录。

## Fail-closed rollback（冻结未来发布，不改写历史）

1. 先 disable full-daily timer，并对正在运行的 full-daily service 发正常 SIGTERM/stop；保留 `TimeoutStopSec=40min` 让 bootstrap 完成 IAM cleanup。禁止 SIGKILL、重启主机或先撤 broker 权限，否则可能打断临时 tagger key 清理。
2. 等 service 完全退出后，用 authenticated IAM readback 确认 tagger 连续两次零 key。若 cleanup 状态不确定，先由受控 broker/operator 失活并删除 tagger 的所有 key，再确认 double-zero；此时不得重新运行 publisher。
3. 移除/隔离 durable→daily 的 `20-full-publication-on-success.conf` 并 `daemon-reload`，确认 daily timer disabled、daily service inactive、durable 没有 `OnSuccess` 指向 daily。witness 和 publisher-only durable timer可以继续；它们不需要 broker/tagger，也不应停止采集。
4. 撤销两个 approval arms。若 broker credential 或 authority 可疑，在 daily service 已退出且 tagger zero 后，失活/删除 broker key并隔离 encrypted blob；不要 decrypt、打印或复制它。六字段 evidence 可保留用于事故审计，但不能被当成可运行凭据。
5. 已写的 exact-version tags、conditional receipts 和 write-once MANIFEST 不得删除、覆盖或“回滚”；rollback 的含义只是冻结后续 publication。不要恢复 legacy standing tagger blob，不要静默切回 copied-v2，不要手动改 immutable runtime symlink。修复须走新的 clean reviewed commit、重新安装和完整 canary。
6. 记录 rollback UTC、触发原因、unit 状态、policy/credential metadata hash、两个 IAM identity readback、tagger double-zero 与 capture health。只有新一轮审计明确批准后才能重新创建 arms 和恢复 full timer。

## 必须通过的正反测试

- W09：能 list research release namespace、能读 MANIFEST；不能读 release 内复制的数据对象。
- W09：不能 list `ec2/warehouse`、`ec2/control`、`ec2/raw`；不能 versionless `GetObject` canonical 数据；任何 raw/RFQ GET、exact GET 或 tag GET 都被拒绝。
- W09：带 receipt 指定 VersionId 且有 eligibility tag 的 warehouse/control 对象可读；无 tag、tag=false、错误 VersionId、非 allowlist 路径都拒绝。
- W09：Put、Delete、Put/Delete Tagging、Restore 全部拒绝。
- publisher delta：只为 canonical 新增精确 `GetObjectVersionTagging`，并只列 manifest version history；文件中不得出现 `ec2/raw`，任何 tag write 都不得授予。
- tagger：`GetCallerIdentity` 必须与审计记录的 IAM user ARN 一致；普通 canonical 精确版本 tag preflight/readback 可用；任何 raw/RFQ tag read/write/delete 都被拒绝。
- tagger：不带 `versionId` 的 PutObjectTagging 拒绝；其他 principal 的 versioned tag write/delete 被 bucket policy 拒绝。
- broker：`GetCallerIdentity` 的 ARN/账号必须精确匹配 evidence；`GetUser/ListAccessKeys/CreateAccessKey/UpdateAccessKey/DeleteAccessKey` 只能作用于固定 tagger user。对其他 user、任何 policy attach/update、user create/delete、S3、publisher、W09 或 RFQ 操作全部拒绝。
- broker/tagger transaction：初始存在任何 tagger key 时只能清理并以 `STALE_KEYS_RECONCILED_RETRY_REQUIRED` 退出；零 key 重跑才可创建一个内存态 key。tagger 子命令结束或被 SIGTERM 后，最终必须连续两次读到零 key；日志和收据不得出现 key ID/secret。
- receipt：首次 `If-None-Match: *` conditional create 成功并返回非 null VersionId；同 key 再写不能产生第二版本；ListObjectVersions 只能看到一个版本且没有 delete marker；随后 exact HEAD/GET 字节验证通过。
- raw prune authority：只列 receipt 已完整 SHA 验证的同日非 RFQ raw exact versions；RFQ 与跨日对象逐对象 deferred/retain。远端小 receipt conditional-create + exact readback 成功以前，不得写本地 `PRUNE_AUTHORITY.json`。
- manifest：首次 conditional create 前 history 必须为空；写后 exact key 只能有刚返回的一个 VersionId 且没有 delete marker；历史版本、delete marker、截断 history、并发 create 和任何 delete 都必须 fail closed。

## single-writer 审计收据

只有在上述测试全部通过后，才可生成 `canonical-eligibility-single-writer-audit-v1`。证据包至少应保存：

- 完整现有/合并后 bucket policy、broker/tagger/publisher/W09 的 inline 与 managed policy 版本、managed-policy attachments、permissions boundary、相关 SCP；broker/tagger 是 IAM user，因此没有 role trust policy；
- broker、tagger、publisher、W09 四个实际 `sts:GetCallerIdentity` 输出，以及 authenticated `GetUser` 返回的 broker/tagger immutable `UserId`；
- 上述正反测试的命令、UTC 时间、request ID、VersionId 与结果；
- 五份 policy、六字段 identity evidence 和 systemd encrypted blobs（只记录 ciphertext）的字节数与 SHA-256；不得保存或 hash 明文 secret payload。

在这些证据存在以前，`exclusive_exact_version_writer`、`versionless_tagging_denied`、`other_automation_tag_writers_denied` 三项不得写成 `true`，也不得运行 eligibility tagger 的批准参数。

上述 single-writer 收据只证明“谁能写标签”，不等于 RFQ 数据获准。RFQ 还需要独立 sealed-evidence 授权；当前没有该授权，因此 RFQ 必须继续 BLOCKED/OFF，并保持 no-repair。

## AWS 官方语义依据

- S3 API 与 IAM action 对照（带 `versionId` 的 GET 使用 `s3:GetObjectVersion`；ListObjectVersions 使用 `s3:ListBucketVersions`；版本化标签 API 使用对应 VersionTagging action）：https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html
- `s3:ExistingObjectTag/<tag-key>` 与 `s3:RequestObjectTag/<tag-key>` 的对象标签访问控制：https://docs.aws.amazon.com/AmazonS3/latest/userguide/tagging-and-policies.html
- `If-None-Match: *` conditional write 与所需 `s3:PutObject` 权限：https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- 用 `s3:if-none-match` 在策略中强制 conditional write：https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html
- AWS 推荐在 resource policy 的 Deny 中使用 `ArnNotEquals` + `aws:PrincipalArn`，以及 role session 的 PrincipalArn 取值：https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html
- IAM 显式 Deny 的评估逻辑：https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html
