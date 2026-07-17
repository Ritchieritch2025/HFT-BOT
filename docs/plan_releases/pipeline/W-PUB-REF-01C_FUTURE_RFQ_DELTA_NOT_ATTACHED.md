# FUTURE RFQ DELTA — NOT ATTACHED

状态：**设计存档 / 当前不可应用 / RFQ DATA_INTEGRITY_BLOCKED**。

当前 base IAM 包在 AWS 层关闭 RFQ：W09 被显式拒绝读取整个
`ec2/raw/*`，tagger 被显式拒绝检查或修改整个 raw namespace 的标签，
bucket policy 对 RFQ exact-version 标签写删不保留任何 principal 例外；
publisher delta 也不授予 RFQ 权限。

这里仅保存未来接口形状，不是可直接应用的 IAM JSON。若以后重新批准 RFQ，
必须另开变更单，先解决并独立审计以下事项，再生成新的策略版本：

1. 用独立、不可自签的证据验证每个 `source_evidence_sha256`，绑定 sealed exact
   `VersionId` 集，并证明不存在 quarantine 或缺失对象。
2. 重新审计 `research-eligible=true` 与 `research-channel=rfq` 双标签合同。
3. 单独为 tagger 增加收紧 RFQ glob 的 exact-version tag GET/PUT，并在 bucket
   policy 中只为当时经验证的 pathless tagger role ARN 解除 RFQ mutation Deny。
4. 单独为 publisher 增加 exact-version tag read；如仍以 `HeadObject` 验证 RFQ，
   必须明确接受 `s3:GetObjectVersion` 同时具备正文读取能力。
5. 单独为 W09 增加双标签 exact-version read，并把当前 raw-read explicit Deny
   替换成缺任一标签即拒绝的边界；不得授予 versionless raw GET 或 raw list。
6. 正反测试、policy 全文、role/SCP/boundary/trust、request ID 与 UTC 时间必须
   形成新的 audit receipt；当前 repair-01~07 或旧 evidence 不可复用。

在新变更被独立批准、审计并实际应用以前，任何脚本都必须继续使用 RFQ OFF，
不得 repair、补数、重跑、加标签或让 RFQ 进入 v3 manifest。
