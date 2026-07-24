# Production PnL lineage

这条入口只做一件事：在固定 W09 上以 instance profile 读取 S3
`GetObjectVersion`，对 PnL runner 将使用的原始记录建立可外部 pin 的
`pnl-spine-lineage-receipt-v1`。它不会写 S3，也不会发单。

## 已固定的 8 份 release

`inputs/EXACT_V3_RELEASES_20260710_17.json` 是 2026-07-10 至
2026-07-17 的八个 exact V3 manifest pin：

- raw SHA-256:
  `eef0c38ff454da87a713d825ed18b665bb1b8fd87aa55e1a3b68807168b7e8f6`
- bucket: `kalshi-vault-ritcardo`
- 每条均固定 manifest key、manifest VersionId、manifest 原始字节 SHA、
  release ID 和日期。

2657 个对象 / 29,473,216,651 字节的清单汇总是输入库存事实，不等于
本次 PnL fixture 已经产生 lineage。producer 会重新读取八个精确
manifest，并要求最终 fixture 的 release/object set 与其逐项完全相等。
旧的 W1 `INPUT_MANIFEST.json` 只能辅助生成候选 fixture，不能冒充最终
runtime fixture。

## 生产运行

先由审核/授权流程分别给最终 runtime fixture 和 record extraction spec
提供独立的原始文件 SHA，并给审核过的 producer 提供外部 canonical
code-bundle SHA pin（它不是 `production_lineage.py` 的 raw file SHA）。
不要在同一条生产命令里临时计算并自我批准这些 SHA。

producer 必须从独立的 root-owned 部署树加载。其源文件必须无任何 write
bit，沿途父目录必须 root-owned 且不可由 group/other 写入，路径中不得有
symlink；运行服务使用非 root 身份。producer 的受审 code bundle 由
`production_lineage.py` 与 `terminal_lineage_bridge.py` 两个文件组成；
当前
extractor code-bundle SHA 是
`57a4b52e984295ed5a4a4272c2b00ee7eedb29b5a40313961eb67bfd939d037b`，
最终仍以独立审计确认的 SHA 为准。

### Parquet runtime receipt

生产环境完全禁止 `parquet-key-v1`。任何 Parquet source 必须使用
`parquet-key-v2`，而且 locator 必须把审核过的 runtime receipt 原始文件
SHA 写入 `runtime_receipt_raw_sha256`：

```json
{
  "schema_version": "parquet-key-v2",
  "match": {"row_id": "EXACT_ROW_ID"},
  "source_fields": ["row_id", "decision_ts_ns", "best_yes_bid_e4"],
  "runtime_receipt_raw_sha256": "APPROVED_RUNTIME_RECEIPT_RAW_SHA256"
}
```

runtime 必须是用 `venv --copies` 建立的专用 Python 环境；不能使用 symlink
解释器，不能继承 `PYTHONPATH`、user site 或环境变量注入。DuckDB wheel
必须先由操作员从已审核的本地 wheelhouse 以 hash 固定安装，不能在这一步
联网取“最新版本”。以下是部署模板（wheel 文件名和 SHA 由审核包替换）：

```bash
sudo python3 -m venv --copies /opt/w09-pnl-parquet-runtime
sudo /opt/w09-pnl-parquet-runtime/bin/python3 -m pip install \
  --no-index --find-links /opt/w09-audited-wheelhouse \
  --require-hashes -r /opt/w09-audited-wheelhouse/duckdb-requirements.txt
sudo chown -R root:root /opt/w09-pnl-parquet-runtime
sudo chmod -R a-w /opt/w09-pnl-parquet-runtime
```

`duckdb-requirements.txt` 必须是类似
`duckdb==EXACT_VERSION --hash=sha256:EXACT_WHEEL_SHA256` 的单一固定版本。
安装冻结后，以非 root 服务身份、`-I -B` 启动审核过的 producer 来生成
一次性 runtime receipt：

```bash
sudo -u w09-research /opt/w09-pnl-parquet-runtime/bin/python3 -I -B \
  /opt/w09-pnl-spine/tools/research/pnl_spine/production_lineage.py \
  runtime-receipt \
  --expected-extractor-code-sha256 57a4b52e984295ed5a4a4272c2b00ee7eedb29b5a40313961eb67bfd939d037b \
  --output /srv/w09-research/runtime-staging/PARQUET_RUNTIME_RECEIPT.json
```

receipt 会记录并现场核对：

- 实际 `sys.executable` 的路径、字节数和 SHA；
- isolated venv prefix、Python version/cache tag 和 `-I -B` flags；
- `duckdb` 全 package tree 的每个文件及 SHA；
- 实际加载的 `_duckdb` native extension 路径、SHA 和版本；
- Python package 与 native extension 的版本一致性；
- DuckDB 单线程、4 GiB 内存上限、零临时 spill、禁止自动安装/自动加载
  extension、关闭 object cache 的实际生效值。

这里的 runtime pin 精确定义为：实际 Python executable 文件、专用 venv
中的完整 DuckDB package tree 与实际 `_duckdb` native extension；它不是
整个操作系统标准库/动态链接依赖的全盘哈希。系统 Python、标准库和动态
依赖仍属于 W09 镜像/补丁治理边界，不能把本 receipt 解读成完整 AMI
attestation。

将 stdout 的 `receipt_raw_sha256` 移到本进程之外独立审核；审核通过后由
root 把 receipt 移入不可由研究服务修改的 control 目录并设为只读。禁止
在同一个命令里现算 SHA 再自我批准。一个 record spec 只能引用一个
runtime receipt SHA。

```bash
sudo -u w09-research /opt/w09-pnl-parquet-runtime/bin/python3 -I -B \
  /opt/w09-pnl-spine/tools/research/pnl_spine/production_lineage.py \
  --fixture /etc/w09/pnl-spine/FINAL_RUNTIME_FIXTURE.json \
  --fixture-sha256 APPROVED_FIXTURE_RAW_SHA256 \
  --manifest-pins "/opt/w09-pnl-spine/Deepresearch V3/pnl_spine/inputs/EXACT_V3_RELEASES_20260710_17.json" \
  --manifest-pins-sha256 eef0c38ff454da87a713d825ed18b665bb1b8fd87aa55e1a3b68807168b7e8f6 \
  --record-spec /etc/w09/pnl-spine/FINAL_RECORD_EXTRACTION_SPEC.json \
  --record-spec-sha256 APPROVED_RECORD_SPEC_RAW_SHA256 \
  --expected-extractor-code-sha256 57a4b52e984295ed5a4a4272c2b00ee7eedb29b5a40313961eb67bfd939d037b \
  --parquet-runtime-receipt /etc/w09/pnl-spine/PARQUET_RUNTIME_RECEIPT.json \
  --parquet-runtime-receipt-sha256 APPROVED_RUNTIME_RECEIPT_RAW_SHA256 \
  --region us-east-2 \
  --output /srv/w09-research/runs/PNL_SPINE_LINEAGE_RECEIPT.json
```

CLI 只允许以下生产身份边界：

- EC2 instance `i-0e53d134dceffe166`；
- instance profile ARN 严格为
  `arn:aws:iam::321572485933:instance-profile/w09-research-runner`；
- role 严格为 `w09-research-runner`；
- IMDSv2 临时凭据；
- region 严格为 `us-east-2`，S3 host 是代码内常量，不能由参数拼接；
- `kalshi-vault-ritcardo` 的 exact `GetObjectVersion`。

静态 access key、AWS profile、web identity 和 container credential
配置任一存在都会拒绝启动。每个唯一 `(bucket,key,VersionId)` 只完整
下载、SHA/size 校验一次；多条 runtime record 可以在验证后 fan-out。
当前 record spec 是 `pnl-spine-record-extraction-spec-v2`：每条记录通过
已排序的 `sources[]` 声明一个或多个带 alias 的 exact object + locator。
`DIRECT` 严格限制为单源；`NORMALIZED_ROW` 必须通过声明式
`field-map-v2` 从明确的 source alias 取值并标记为 `DERIVED`。每个声明
的 alias 都必须被转换实际使用，L1/L2/TRAIN 阈值工件等多源输入会全部
进入 `source_members` 和 `input_set_sha256`。源定位、转换配置、代码字节
和最终 record hash 都会进入 receipt。library 保留 v1 单源兼容只用于
离线测试；生产 CLI 遇到任何非 v2 spec 会直接拒绝。

producer 在任何 manifest/S3 读取前先核对外部代码 pin，并在 receipt
盖章前再核对一次。两次核对之间的 TOCTOU 由上述 root-owned、只读、无
symlink 部署树及非 root 服务身份收口。输出同样是 create-once：如果目标
已经是普通文件或 symlink 都会拒绝，绝不覆盖旧 receipt。重跑必须使用一
个新的、此前不存在的输出路径。

如果 spec 没有 Parquet locator，CLI 会拒绝多余的 runtime receipt 参数；
如果 spec 有 Parquet locator，缺路径、缺 raw SHA、locator 与 CLI SHA
不一致、receipt 自哈希错误、live Python/DuckDB 任一字节或版本变化、
Python 不是 `-I -B`、runtime 不是 root-owned read-only，都会在构造 S3
reader 之前 fail closed。

运行成功后，stdout 会给出：

- lineage receipt 的 canonical SHA（供 trusted authority 绑定）；
- receipt 文件原始字节 SHA（供 runner CLI 的外部 pin 参数）；
- verified logical object 与 record 数量。

这两个 SHA 必须被移到 producer 进程之外保存/批准，然后才能交给
`tools.research.pnl_spine.runner`。producer 自己的输出不能自我授权。

## Official terminal external-root channel

原始 8 个 V3 release 继续作为 L1/L2/trades 的唯一 exact-version
lineage。若它们的 settlement catalog 对目标 ticker 为零覆盖，可额外使用
`pnl-spine-record-extraction-spec-v3` 和
`pnl-spine-official-terminal-root-spec-v2`。这条通道不复制或写入 S3；
它只离线读取 W09 上 root-owned、无 write bit、全路径无 symlink 的：

- 每个 shard 的 exact adapter source code；
- 每个 shard 的只读 authority；
- `CAPTURE_RECEIPT.json`；
- `RAW_PINS_XX.json`；
- receipt 声明的每一份原始 HTTP response；
- normalized terminal output。

spec 必须列出完整、排序、唯一且非空的 `required_tickers`。所有 shard 的
adapter code 必须现场重哈希；authority/capture/raw-pins/normalized
ticker 集合必须逐项等于它；少一个、
多一个、重复一个或零覆盖都会拒绝。唯一例外是已由官方 raw response
证明不符合冻结 A01 资格的 ticker：它必须逐个进入
`eligibility_exclusions[]`，并使用与经济记录同等级的完整链：
exact adapter code、singleton authority、capture receipt、raw pins 和
receipt 中的全部 root-owned raw response。只提供一份“长得像官方响应”
的 JSON 会被拒绝。每项还必须绑定 exact code/config/SHA/size，并明确：

```json
{
  "reason_code": "NON_STANDARD_PRICE_LEVEL_STRUCTURE",
  "observed_price_level_structure": "tapered_deci_cent",
  "http_status": 200
}
```

这类 ticker 计入完整 denominator，但不得进入 `terminal_record_index` 或
任何经济/PnL row。`eligible_tickers + eligibility_exclusions` 的无重叠并集
必须逐项等于 `required_tickers`。默认
`SINGLE_VERSION_REQUIRED` 禁止混用 adapter。647 个旧版成功 ticker 与
200 个新版重采 ticker 这类混合只能显式使用：

```json
{
  "mode": "EXPLICIT_PER_SHARD",
  "approved_shards": [
    {
      "shard_id": "batch-00",
      "adapter_code_sha256": "EXACT_CODE_SHA",
      "adapter_config_sha256": "EXACT_CONFIG_SHA"
    }
  ]
}
```

`approved_shards` 必须按 shard_id 排序，并与 `shards[]` 的每个
code/config pin 完全相等；它不是通配许可。v3 的 settlement source 只能是：

```json
{
  "kind": "SETTLEMENT",
  "record_id": "EXACT_SETTLEMENT_ID",
  "mode": "DIRECT",
  "sources": [
    {
      "alias": "official_terminal",
      "source_class": "OFFICIAL_TERMINAL_CAPTURE",
      "ticker": "EXACT_MARKET_TICKER"
    }
  ],
  "transform": {"schema_version": "identity-v1"}
}
```

对应 fixture evidence binding 使用
`source_class/market_ticker/source_raw_response_sha256`，不能伪装成一个 V3
release object。成功输出是 `pnl-spine-lineage-receipt-v2`：保留原始
release-set SHA 和 exact object reads，同时把 terminal root receipt、每个
shard 的 adapter code/config、control/raw/normalized SHA、完整 ticker
coverage、完整 normalized record/metadata rows 及其 hash 纳入同一个
外部 pin。root receipt 版本为
`pnl-spine-official-terminal-root-receipt-v2`，并记录操作员批准的
terminal-root spec 原始字节 SHA。

bridge 不信任 normalized payout。它独立重放固定 Kalshi origin、
current→historical-only-on-404、429 bounded retry、250ms throttle、
authority wall-clock window 与 attempt 顺序；随后重新解析每个 selected
raw response，并把官方 result、exact payout、settlement timestamp、
tick table 和 lifecycle observation 与 normalized settlement/metadata
逐字段比对。任何自洽地改 pin、把 YES 改成 NO、把 URL 改到其他域名或把
fetch 移出 authority window 的尝试都会 fail closed。

该通道的 temporality 永远是
`OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF`。它只解除 finalized YES/NO 与
exact payout 的 settlement 覆盖，不会解除以下门：

- historical point-in-time metadata；
- authoritative scheduled start；
- historical lifecycle/tick intervals；
- exchange settlement revision sequence。

生产 CLI 额外传入：

```bash
  --terminal-root-spec /etc/w09/pnl-terminal/TERMINAL_ROOT_SPEC.json \
  --terminal-root-spec-sha256 APPROVED_TERMINAL_ROOT_SPEC_RAW_SHA256
```

所有 external-root 文件在构造 S3 reader 之前完成本地重哈希和权限验证；
bridge 本身记录 network reads = 0、S3 writes = 0、financial mutations = 0。
