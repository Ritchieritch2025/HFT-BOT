# W-RFQ-FRESH-01 — 新鲜 RFQ 独立研究授权与执行计划

日期：2026-07-17

状态：**已授权，但所有研究发布硬门默认关闭；不是旧 RFQ 数据解封令，也不是
research bucket / tagger / publisher 写入令**

## 1. 操作员原文授权

> 批准重新开放新鲜 RFQ 数据研究；旧受损数据继续 DATA_INTEGRITY_BLOCKED，禁止修复；从新采集数据建立独立 RFQ 清单，并结合 L1/L2/盘口在 W09 分析。

授权文本 SHA-256（只取上述中文正文的 UTF-8 字节，不含 Markdown `>`、首尾空白或
末尾换行）：`8aaa7e40f4d1cb414d7b06be213aa10dc37bcc4d054c729d3e96eb1c884ef66e`。

本文件只按上述原文执行。“重新开放”仅指在新的、严格 post-stop T0
之后采集并通过全部质量门的 RFQ 数据。它不改变任何历史终止令或隔离裁决。

## 2. 永久边界

1. `deep01` 历史 RFQ lineage、原 284-object set、282/284
   quarantined derivative，以及从这些对象派生的 receipt、cache、report 或
   manifest，永久保持 `DATA_INTEGRITY_BLOCKED`。
2. 对上述 lineage 禁止 repair、逐行修补、整对象替换、补数、重跑、重新封印、
   重新打标签、进入新 manifest，或作为新鲜 lane 的证据来源。
3. `repair-01` 至 `repair-07` 只可保留为历史审计材料；`repair-08` 及任何新
   repair 均不执行。
4. 新鲜 lane 必须使用独立 ID、目录、receipt set、S3 exact-VersionId set、
   eligibility evidence 和 W09 cache。不得与 `deep01` 共用可写状态。
5. RFQ 重新开放不改变 L1、L2、trades、盘口图谱的 canonical base；RFQ 只能以
   独立 overlay 与已经封印的 base 绑定。

旧集合的机械 deny authority 使用 repair-01 前的 284-object identity（文件
SHA-256 `66144229b301d8d814663f5adbaff6b5f060cc61d486ebdd0183b3ce0217f55b`）。
其 284-object set SHA-256 是
`1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71`；
`8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1`
只是 repair-02 selection fingerprint，禁止把两者混作同一集合摘要。

## 3. 已证实的当前状态

2026-07-17 的只读生产审计确认：

- `2026-07-16` 虽已有 `full_v2` seal，且 seal 所列 RFQ 对象在 S3 metadata
  对照中为 0 missing、0 size mismatch，但 24 个小时只有 **5/24 strict
  PASS**；该日明确拒绝进入新鲜 RFQ lane。
- `2026-07-17` 尚未形成完整日 seal，并且当时只有部分小时证据；不得提前入选。
- 当前 capture 出现大量 256 MiB shard、多次同小时 receipt、订阅 ACK/hour-open
  缺失及 `EVENT_COMPLETENESS_UNPROVEN`。因此 capture shard/supervisor 修复是
  数据采集硬前置，不是报告阶段可以忽略的告警。
- 公开 communications feed 能观察公开 RFQ 机会及生命周期，但不提供完整的
  私有 quote/fill 经济结果。

## 4. 严格 T0

T0 必须同时满足：

1. 晚于旧 RFQ 终止令的生效时刻；
2. 晚于 shard/supervisor 修复部署并验收成功的时刻；
3. 是一个在 T0 前通过 create-only（本地 `O_EXCL`）预提交的 UTC 整点；不得在
   看见数据后回填、换代或覆盖；
4. T0 authority 绑定 fresh lane ID、操作员授权摘要、部署 commit、预生成的
   deployment generation ID、capture template、read-only credential scope 声明和
   完整旧 284-object deny identity；旧 lineage 只能作为拒绝证据，不能作为新 lane
   的输入引用；
5. 每个 v3 hour receipt 都必须绑定该 authority SHA、fresh lane ID、部署 commit 和
   deployment generation ID，并另行记录实际 supervisor PID、child PID 与 child
   generation。

不得把历史文件的最早时间、服务重启时间或文件 mtime 推断为 T0。没有明确 T0
receipt 就没有新鲜 lane。

## 5. 执行阶段

### Phase A — 修复 capture shard 合同

在继续计时前完成并独立审计：

- 每小时 receipt 枚举该小时的 base 文件及全部 numeric shards；
- 每个成员记录相对路径、size、SHA-256 和稳定关闭证据；
- hour set digest 在函数内部按 `(ordinal, relpath)` 排序后计算，拒绝缺号、重复和
  不安全路径，禁止只 hash base 文件；
- 同一小时只允许一个 receipt generation；出现重试、重复或相互冲突的 receipt，
  该小时直接失格。本阶段不允许用事后 supersession 把失败小时改成 PASS；
- 每个 child generation 的初始订阅 ACK、每小时 `hour_open`、持续的已认证订阅状态、
  无 reconnect/disconnect/drop/write error、边界稳定窗口全部为硬条件；持久连接的
  后续小时不强迫重新订阅，但必须保持同 generation 且无任何 invalidation；
- final parser 的 complete-line EOF 必须与最终 attested shard set 逐成员双向相等；
  torn tail、迟到 append 或新 shard 一律不得签出 PASS；
- v3 validator 必须机械验证 expected hour boundary、raw/metrics health、counter
  deltas、完整 EOF 和 shard exact identity；不能只相信顶层 `status=PASS`；
- 离线回归覆盖多 shard、边界轮转、进程重启、迟到 append、重复 receipt、缺 shard
  和 torn shard。

生产部署或服务重启仍需正常变更收据；本文件不自行扩大运维权限。

### Phase B — 从 T0 重新采集

从 T0 开始只积累新的 RFQ hour generations。任何小时出现 FAIL、
`EVENT_COMPLETENESS_UNPROVEN`、`PARTIAL_START`、receipt 歧义、ledger 无效 JSON、
缺 shard 或 seal 不一致，该候选日立即失格；不得用相邻小时补齐。

首个可入选日期 D 必须满足：

- D 的 `00` 至 `23` 共 **24/24** 小时全部 strict PASS；
- D+1 的 `00`、`01` 两个 cross-day watermark 小时也全部 strict PASS；
- D 为 `full_v2 / SEALED`，D 的 24 个 analysis-hour RFQ capture member set 与
  seal 中按路径日期定型后的 D-analysis capture subset exact 相等；
- 现行 `full_v2` 的 `receipt_cross_day_hours=2` 会把 D+1 `00/01` capture
  作为 cross-day watermark members 物理列入 D seal。它们必须另行标记为
  `WATERMARK`，禁止计入 D 的24小时 analysis set；
- 每个对象均有非空 VersionId、size 和完整 SHA-256 证明；
- 无 quarantine、repair lineage、重复 logical key 或未解释的额外对象。

D+1 的 `00/01` 是 watermark evidence，不属于 D 的 analysis object set。
它们可以作为现行 D `full_v2` 的 cross-day members，但这只表示 D seal
使用了两小时跨日水位窗口，不等于 D+1 full-day sealed，也不得把它们
计入 D 的 24 小时分析。它们必须有独立 v3 receipts 和 exact
VersionId/size/SHA；最后一个 D+1 `01` segment 的 receipt 所在 D+1 `_02`
container 独立绑定，不应虚假声称是 D seal member。如果 D+1 full-day
seal 已存在则可额外绑定，但等待整个 D+1 结束不是放行前置。

找不到满足条件的 D 时继续采集；不得降低到 23/24，也不得发布“近似完整”研究。

### Phase C — 建立独立 RFQ overlay 清单

为合格日期 D 生成 `W-RFQ-FRESH-01` 独立 manifest，至少绑定：

- T0 receipt 与 capture deployment commit；
- 两个明确分离的集合：D 的 24 个 `analysis_hours`，以及 D+1 `00/01` 两个
  `watermark_hours`；
- 26 个 strict PASS hour receipts，以及它们所在 receipt-container 的 exact
  identities；receipt container、capture object 与 seal subset 不得混成一个集合；
- D analysis RFQ exact-VersionId object set 及 set digest；watermark exact set 另列；
- D 的 canonical seal SHA 和 canonical base receipt set SHA；
- L1、L2、trades、dated dim、catalog/盘口图谱的 exact base bindings；
- RFQ market ticker 到 L1/L2 market universe 的确定性映射；
- overlay/base 的 UTC as-of cutoff、允许的前后事件窗口和缺失映射清单。

RFQ overlay 不复制或改写 base 数据。映射失败的 RFQ 记录保留在 DQ ledger，不能
静默丢弃，也不能借用其他日期的盘口。

### Phase D — IAM 核对前保持研究发布零写

最新“重新采集新鲜 RFQ”授权允许现有生产采集者继续按原有 IAM，把已关闭小时写入
canonical `ec2/raw` 来源前缀；这是建立新鲜 source data 的必要生产摄取，不是
research 发布。现有小时同步 timer 不因此停机，也不得扩大其身份、目的前缀或动作。

除此之外，在独立 IAM 审计完成前只允许本地 freeze/plan 和 AWS read-only
list/head/exact-version verification：

- 禁止任何新 research/control receipt 或 manifest 的 S3 PUT/COPY/DELETE；
- 禁止向 research bucket 复制 RFQ、L1、L2 或盘口对象；
- 禁止任何 object/version tag 写入；
- 禁止发布 RFQ durable receipt 或 v3 manifest；
- 禁止启动 W09 RFQ canary。

如果操作员所说的“AWS 零写入”意图也包括停止既有 canonical producer，则必须先
单独停用 `kalshi-s3-sync-hourly.timer`；在得到该明确指令前，本计划不把“重新采集”
解释成“只留在 EC2 本地且不进入 canonical source”。

IAM 审计必须证明 base RFQ hard-off 不被偷偷放宽，并为新 lane 提供独立、收紧的
principal 和资源范围。

### Phase E — 独立 tagger、IAM 与 W09 canary

通过新的单独变更单执行：

1. 独立 RFQ tagger 只消费合格 fresh manifest，并只对其中列出的 exact
   VersionId 写入 `research-eligible=true` 与 `research-channel=rfq`；
2. IAM/bucket policy 对 versionless raw GET、未双标签 exact GET、raw list、
   非专用 tagger 的 tag mutation 保持显式拒绝；
3. publisher 只读取已验证的双标签 exact versions；
4. W09 使用独立 RFQ cache/release ID，先做最小 canary，证明不读取 `deep01`、
   不回退 versionless GET、不污染主机或 v2 cache；
5. canary PASS 后才允许扩大到日期 D 的完整 RFQ overlay 分析。

任何 IAM 证据、双标签、exact GET 或隔离检查失败都立即回到 RFQ OFF；不得临时
绕过 policy。

### Phase F — W09 分析范围

允许分析的内容是 RFQ 机会与市场反应，例如：

- 请求数量、合约数、目标成本、combo/single、创建到删除生命周期；
- RFQ 前后 L1 spread、mid、盘口厚度和 L2 liquidity 变化；
- 请求后价格漂移、流动性撤单和 adverse-selection proxy；
- 不同盘口状态下的机会密度与可观测响应差异。

公开 communications 数据不能证明报价被提交、被接受或成交，也没有完整的私有
成交价、方向、数量和费用。因此它只能支持“机会/逆向选择 proxy”，不得输出真实
毛利、净利润、fill rate 或可执行 PnL 声明。

真实毛利研究必须另行授权并接入可审计的私有 quote/fill 数据，至少包括 RFQ/quote/
fill 关联 ID、我方 side、报价与成交价格、数量、费用、结算或退出价值以及时间戳。
缺少任一关键经济字段时，gross profit 必须为 `null`，不能用公开删除事件或盘口
mid 代替成交。

## 6. 最终放行条件

只有以下证据全部存在时，状态才可从 `AUTHORIZED / GATED` 改为
`READY_FOR_W09_RFQ_RESEARCH`：

1. shard/supervisor 修复及生产验收收据；
2. 明确的 post-stop T0 receipt；
3. 首个 D 的 24/24 加 D+1 `00/01` strict PASS；
4. D seal 中定型的 D-analysis RFQ capture subset 与 24 个 analysis hours exact
   相等；两个 cross-day watermark members 与 analysis set 逻辑分离，receipt
   containers 和 L1/L2/base bindings 各自精确绑定且不混集；
5. AWS IAM/tagger/bucket-policy 独立审计 PASS；
6. RFQ 双标签 exact-version W09 隔离 canary PASS；
7. 报告模板明确区分公开 proxy 与私有 realized economics。

在此之前，允许继续修复 capture 基础设施和积累新数据，但不得把任何日期描述为
可用 RFQ research lane。
