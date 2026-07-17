# DETECTION RUN — 管道与测试基建六项实测

日期：2026-07-17（UTC）
交付截点：2026-07-17T15:37:23Z（操作员要求立即交付）
分支：`plan-sports-market-dynamics-v2`
生产：`ubuntu@3.130.232.109:/home/ubuntu/hft-bot`
约束：生产只读；未改采集、未发订单、未杀 DuckDB 持锁进程、未改生产文件。

## 一行裁决

六项里，`make check` 假绿已修复并单独提交；L2 detector 已从“任意整数就
参与连续性”升级为 outer/raw 双重身份、重复键、marker 与生产类型边界校验。
加固版对 07-14 的 `38,450,029` 行拒绝 `232,158` 行（`0.603791482%`），拒绝
集中在 `l2_00`；在其余 23 小时的 v3-accepted 行上测得 `74` 个 sequence-gap
event、`11,420` 帧 missed。唯一 loss marker 在未隔离的 `l2_17`，总值也为
`11,420`；这是总量一致，不是 74 个 gap 的逐条归因。全日精确 gap 数不可恢复。

RFQ 封存防护代码已在生产 HEAD，但当前 checkpoint 在持续 DuckDB 写锁下不可
观测，结论是 capture growth measured PASS、checkpoint advance UNPROVEN；这不
等于 checkpoint 已失败。生产 tracked tree 干净，但 runtime 依赖至少 `7` 个
未跟踪 deploy artifacts 以及未锁定依赖的 untracked `.venv`，因此当前实际部署
不能只从 HEAD 重建。07-12～16 四个旧数据近似值全部在
`1%` 内。readiness 的执行身份 bug 已修复；重新登记的 `77` 项中 `76` pass、
`1` fail。原先被全仓 pytest 假身份掩盖的 `test_kalshi_golden` 现在因官方
OpenAPI/AsyncAPI 相对既有 baseline 发生未审阅 hash change 而拒绝运行；未接受
新 baseline，所以 core 与 overall lifecycle 都保持红灯。
07-17 seal 的自动首个合理窗口尚未到达，因此第 3 项以 PARTIAL 交付。

## 证据索引

| ID | 证据 / 命令 | 时间 |
|---|---|---|
| E1 | 临时把退出 `23` 的 `tests/_forced_fail_make_check.py` 加入旧 `PY_WAREHOUSE_TESTS`，执行 `make check`；修 Makefile 后用同一探针复跑；随后删除探针。探针只存在于本会话且从未提交 | 2026-07-17，本会话终端实录 |
| E2 | `git show 59222f4:Makefile`；当前 `Makefile:376-386`；`tests/test_make_check_contract.py`；`git show --stat 5c6db3e` | 本地仓库 |
| E3 | 生产 `work/event_packs/l2_gaps_2026-07-14.json`（SHA-256 `86421807eb326f2799e68b9ae21070f81e4976ac1eaec9891ba9074c8bdd8460`）与 `work/quality_log.ndjson` 的 2026-07-16T20:51:07Z 行 | 2026-07-17 只读 |
| E4 | `docs/plan_audits/evidence/DETECTION_RUN_2026-07-17_L2_V3_SUMMARY.json`（SHA-256 `92cd9d1b7695d89bb01a92f4902368410cfb4de17c48679b089797c3f6190287`）；完整 stdout receipt payload SHA-256 `37ad18b4…720a4`；S3 key/size/ETag manifest SHA-256 `4a26bd63…38591` | 14:46:29Z～14:56:13Z，583.831s |
| E5 | 生产 `tools/ingest.py:565-615`、`tools/export_day.py:236-313`、对应测试与 `git merge-base --is-ancestor`；RFQ 文件 `stat` 多次采样；DuckDB `read_only=True` 查询 checkpoint 并按 D6 等待重试 | 13:48:16Z～14:02:19Z |
| E6 | 生产 `git status --porcelain=v1 --untracked-files=normal`、`git diff --name-status`、`git log -1`、`git stash list`、untracked manifest 哈希、systemd/进程引用交叉检查 | 13:46:42Z～14:02:19Z |
| E7 | §5 内嵌 exact glob/SQL；`/home/ubuntu/hft-bot/.venv/bin/python` 3.12.3 + DuckDB 1.4.5，`:memory:`、2 threads、4GB，仅 `read_csv/read_parquet` 归档分区 | 13:46:43.480511Z～13:46:49.051938Z |
| E8 | `docs/plan_audits/evidence/DETECTION_RUN_2026-07-17_READINESS_SUMMARY.json`（SHA-256 `e776bb2a9e5f1963d19c9114281eec23d59dd1af8cbfc855650286496074c85b`）；`work/test_results_latest.json` / focused logs；官方 spec 只读刷新，未接受 baseline | 12:59:10.661Z～15:02:42Z |
| E9 | 生产 `tools/pipeline_supervisor.sh:439-485,539-550` 与 14:20:36Z/14:28:53Z precondition baseline；07-17 seal 文件当时不存在 | 15:37:23Z 操作员要求立即交付，02:10Z seal 终证未执行 |

所有百分比均在本文给出分子/分母；不是从图表读取。

## 1. `make check` 掩盖失败

### 复现与修复

| 实测 | 结果 | 出处 |
|---|---:|---|
| 旧 recipe 下，临时探针显式 `exit 23` | `make check` 返回 `0` | E1；旧 `Makefile:376-377` 是 `test | tail -1`，shell 返回最后一个进程 `tail` 的状态 |
| 修复后，同一临时探针仍 `exit 23` | `make check` 返回 `2`，make 显示 `Error 23` | E1；make 自身把 recipe 的 `23` 映射成顶层 `2` |
| 永久 contract test | `2/2` 通过 | E2；`python3 tests/test_make_check_contract.py` |
| 探针删除后首轮 | `make check` 通过；`tests/run_pipeline.sh` 显示 `PIPELINE PASS` | E1 |

修复位于 `Makefile:376-386`：每个子测试输出进独立 scratch log，显式保存子进程
退出码，失败时先显示最后一行再 `exit $rc`。这不是靠日志文本猜失败，而是保留
真实进程状态。永久测试同时覆盖“失败必须非零”和“成功必须为零”。

独立提交：`5c6db3e fix make check exit-code propagation`，只含 `Makefile` 和
`tests/test_make_check_contract.py`（E2）。临时必败文件已删除且不在提交中。

结论：PASS，关闭 D2/E1 假绿缺陷。

## 2. `l2_gap_check` 数字拼接污染

### 旧证据精确复现

旧 receipt 身份：`86` 个文件、`20,318,050,056` bytes、`38,450,029` 非空行，
生成于 `2026-07-16T20:51:07Z`；全部来自 E3。

| 旧字段 | 旧值 | 出处 |
|---|---:|---|
| `parse_errors` | 26,881 | E3 receipt / quality-log |
| `seq_gap_events` | 123,490 | E3 receipt / quality-log |
| `seq_missed_total` | 11,223,544,809,429,641,370,344,382 | E3 receipt / quality-log |
| `seq_regressions` | 120,740 | E3 receipt / quality-log |
| `stream_restarts` | 76 | E3 receipt / quality-log |
| recorder `loss` marker | 1 | E3 receipt / quality-log |
| recorder `transport_close` marker | 25 | E3 receipt / quality-log |
| `markers_lost_frames` | 11,420 | E3 receipt / quality-log |

这里不是 Python 机器整数溢出；Python int 没有该上限。真正问题是双 writer 的字节
块交错把十进制文本拼接，旧 targeted parser 又接受任意长度 digit-run 并写回
`last_seq`：

| S3 原行 | 相邻值 | 坏行 | 旧算法单次虚增 | 出处 |
|---|---:|---:|---:|---|
| `l2_00.ndjson:34632` | 27,090 | envelope 首值 270,913,987,261,794,270,075；同行第二 envelope/raw 为 7,540 | 270,913,987,261,794,242,984 missed | E4，S3 流到 `awk NR=34629..34636` |
| `l2_00.ndjson:589894` | 174,675 | envelope 174,676,415,217；raw 415,217 | 174,676,240,541 missed | E4，S3 流到 `awk NR=589892..589896` |

第二例仍小于 uint64，且 envelope 内只有一个 `source_sequence`，所以“只加 uint64
上限”或“只拒绝重复 key”都不够。

### 修复

最终 detector（`tools/l2_gap_check.py`）实施以下顺序：

1. outer JSON 与 escaped raw JSON 都做完整解析，outer/raw 任一重复 key 均拒绝；
2. outer 必须符合 RawLogWriter 字段集合，`source=Kalshi`，时钟为非负 int64；
3. `sid`/`seq` 按生产 `uint64`、`stream_epoch` 按生产 `uint32` 校验；
4. envelope/raw 的 `sid`、`seq` 必须成对存在且完全相等；orderbook message 还必须
   有非空且一致的 ticker；
5. marker 也走 outer schema、重复键、时钟、raw-empty 与 loss-count 语义校验；
6. 任一拒绝行不得更新 `last_seq`，并切断该 base 的局部连续性；
7. 只要一个小时 base 出现拒绝行，该 base 的 gap/missed/regression 与 marker 都
   单独放进 `*_discarded_untrusted`；receipt 明示 whole-day lower-bound/incomplete。

第一提交 `50d0999 fix L2 gap counters on corrupt rows` 关闭 decimal splice 污染；
独立复核后又以 `bc70b6b harden L2 receipt validation and audit provenance` 修正 marker
绕过、类型边界、raw duplicate、ticker presence、marker base 归属，并加入只做
ListObjectsV2/GetObject 的 stdout audit adapter。focused suite 最终为 `17/17`
通过（E4；`17 passed in 0.03s`）。合法 `1 -> 9` 仍是 `1` 个 gap / `7` missed。

### 07-14 只读全量重跑

输入身份与旧 receipt 精确一致：`86` files、`20,318,050,056` bytes、
`38,450,029` lines（E3/E4）；因此不是换了数据集。

| 新字段 | 值 | 解释 / 出处 |
|---|---:|---|
| v3 `corrupt_rows` / `parse_errors` | 232,158 | E4；当前规则拒绝率 `232158 / 38450029 * 100 = 0.603791482%` |
| `corrupt_sequence_rows` | 8,538 | E4；detector 保守标记为可影响 sequence/marker identity，率 `0.022205445%` |
| untrusted base | `l2_00`（1/24 小时 base） | E4；唯一出现 v3 rejection 的 base |
| untrusted 输入 | 4 files、1,655,347,606 bytes、3,102,656 lines | E3/E4，同一 `l2_00` 对象集 |
| v3-accepted bases | `l2_01`～`l2_23`（23/24） | E4 `trusted_file_bases`；“accepted”不等于证明不存在所有未知缺陷 |
| accepted-base `seq_gap_events` | 74 | E4；只含上述 23 bases |
| accepted-base `seq_missed_total` | 11,420 | E4；只含上述 23 bases |
| accepted-base `seq_regressions` | 0 | E4 |
| accepted-base `stream_restarts` | 0 | E4 |
| `sids_total` | 24 | E4 |
| `sids_with_seq_gaps` | 1 | E4；只计 accepted bases |
| trusted recorder lost frames | 11,420 | E4；唯一 loss marker 位于 `l2_17`；`l2_00` 为 0 |
| recorder markers（all） | loss 1；transport_close 25 | E4；其中 `l2_00` 有 2 个 transport_close，已隔离 |
| quarantined `l2_00` 候选 gap | 3,168 | E4；受拒绝行断点影响的诊断值，禁止解释为真实 gap |
| quarantined `l2_00` 候选 missed | 1,161,280,190 | E4；诊断值，禁止解释为真实 lost frames |
| quarantined `l2_00` 候选 regression | 3,186 | E4；诊断值 |

`232,158` 是 v3 当前规则拒绝的行数，不宣称穷尽所有可能 corruption。原因精确
分解如下；合计为 `232,158`（E4）：

| 原因 | 行数 | 原因 | 行数 |
|---|---:|---|---:|
| outer JSON invalid | 133,140 | raw JSON invalid | 51,687 |
| duplicate envelope key | 16,677 | incomplete envelope | 9,582 |
| raw member count invalid | 7,777 | duplicate raw key | 5,753 |
| ticker mismatch | 4,342 | unknown envelope key | 1,353 |
| missing orderbook ticker | 787 | channel mismatch | 503 |
| sequence identity mismatch | 224 | invalid receive wall ns | 151 |
| incomplete sequence identity | 75 | invalid receive mono ns | 51 |
| invalid envelope | 48 | invalid sequence value | 8 |

旧 `26,881 / 38,450,029 = 0.069911521%` 只代表旧 parser 连 channel/sid/seq
都抠不出的行；两条实际污染 missed 的 splice 行反而不在其中。因此 SESSION_LOG
旧称“坏行 26,881”必须降级为 legacy-unparsed 口径，不能再当全部坏行。

结论：detector FIXED；07-14 的 23 个 v3-accepted 小时是 `74` gaps / `11,420`
missed，且与 `l2_17` 的 loss marker `11,420` 总量一致。receipt 只支持 aggregate
equality，不支持逐 gap 归因。全日 24 小时的精确 gap 数不可从已损坏的 00 时文件
恢复；`74` 是在当前校验规则下的 whole-day lower bound，不把 unknown 冒充 0。

## 3. RFQ checkpoint 与 07-17 封存

### 07-14 根因与已有防护

生产分支 `codex/pipeline-recovery-hardening` 的 HEAD 为
`e287778d30b56170f427d2b03eb17ee31e61a5ec`。两项防护提交都为该 HEAD 祖先
（E5）：

| 提交 | 防护 | 出处 |
|---|---|---|
| `5c6d7b9` | RFQ checkpoint 只推进到完整换行边界；按 shard 分流；同事务提交 | E5，`tools/ingest.py:565-615` |
| `ef85dea` | checkpoint 后没有未消费完整行即可封存；partial trailing record 与合法 0-byte 不再永久卡门 | E5，`tools/export_day.py:236-313` |

`tests/test_export_day.py:326-348` 静态覆盖 partial PASS、未消费完整行 FAIL、
checkpoint==size PASS、0-byte+None PASS。生产现有唯一 `test_export_day` 收据是
2026-07-10 的 `22/0`，早于 `ef85dea` 的 2026-07-14T16:25:20-04:00；所以
“代码和测试存在”已证，“生产 post-fix 测试执行收据”仍缺（E5）。07-14 最终 seal
由 `e287778` 在 2026-07-16T20:13:37Z 生成，`483` 个 raw file 全部
checkpoint==size；它证明最终能落封，但没有实际命中特殊 partial/0-byte 分支（E5）。

另用正式 RFQ 异常分类脚本只读演练 07-16 seal：该 seal 有 `372` 个 raw、
`324` 个 archive，其中 RFQ family `160` 个；checkpoint null=`0`、
equal-size=`160`、less-than=`0`、greater-than=`0`、partial-only=`0`、
unconsumed-newline=`0`（E5，本会话终端）。这验证了最终终证脚本的分类口径，但仍不是
当前正在写入 shard 的实时 checkpoint 证据。

### 当前写入与 checkpoint 可观测性

| UTC | 文件 / inode | bytes | 出处 |
|---|---|---:|---|
| 13:48:16 | `rfq_13.ndjson.4` / 6291645 | 6,434,816 | E5 `stat` |
| 13:49:45 | 同上 | 43,192,320 | E5 `stat` |
| 13:51:31 | 同上 | 86,556,672 | E5 `stat` |
| 13:53:57 | 同上 | 148,422,656 | E5 `stat` |
| 13:56:46 | 同上 | 222,158,848 | E5 `stat` |
| 14:01:18 | `rfq_14.ndjson` / 6291648 | 26,585,698 | E5 `stat` |
| 14:01:49 | 同上 | 39,426,658 | E5 `stat` |
| 14:02:19 | 同上 | 52,619,874 | E5 `stat` |

这些 stat 样本属于 `MEASURED_THIS_SESSION / terminal-only`，文件继续轮转后不能
当耐久 receipt 复算；它们支持 capture growth PASS。可是 13:48:16Z 与
14:02:10Z 的只读 DuckDB 查询，
以及其间持续至少 `9m47s` 的短间隔重试，全部被 ingest PID `695727` 的正常写锁
拒绝；按 D6 未杀进程、未停服务。ingest/capture stdout、systemd journals、
RFQ metrics/state/segments 均无 staging checkpoint offset；`rfq_segments` 的
`metrics_evidence.end_offset` 是 metrics 文件游标，不能冒充 ingest checkpoint
（E5）。

因此当前结论必须是：`RFQ_CAPTURE_GROWTH=MEASURED_PASS_IN_AUDIT_WINDOW`；
`RFQ_CHECKPOINT_ADVANCE=UNPROVEN`；审计读取路径在持续 writer lock 下不可观测。
这不等于 checkpoint 已失败，也不能用文件增长替代 checkpoint 证据。

### 07-17 落封终证窗口

14:20:36Z 基线：`date=2026-07-17.json` 不存在，export log 中 07-17 的
EXPORT/SEAL/VERIFY terminal 行均为 `0`，这在当天尚未结束时是预期状态（E9）。
14:28:53Z 再查 seal 文件仍不存在（E9）。

生产 HEAD 的 `tools/pipeline_supervisor.sh:439-485` 还明确规定：UTC hour `>=2`
才做 caught-up → export → seal，`02:00Z` 是自动首个合理尝试窗；`03:00Z` 才写
`work/live/seal_alarm.json` 作为持久失败报警。因此 `00:10Z` 不存在 seal 本身是
预期观察，不得误报成失败（E9，生产 HEAD 静态代码）。

`00:10Z` 只能复核“日界后尚未到自动 attempt 窗”，不是终证。原计划在
`2026-07-18T02:10Z` 或首次 automatic attempt 后核验 seal、RFQ checkpoint 分类
和 VERIFY，并最晚跟到 `03:00Z` alarm；操作员于 15:37:23Z 要求立即交付，哨兵已
停止。因此本版没有 07-17 seal 终证，第 3 项保持 PARTIAL；后续若补证，应直接从
02:10Z 后只读检查继续，不必重做前五项。

## 4. 生产 Git 状态与 HEAD 可重建性

14:02:19Z 最终复核（E6）：

| 项目 | 精确结果 | 出处 |
|---|---|---|
| branch | `codex/pipeline-recovery-hardening` | E6 |
| HEAD | `e287778d30b56170f427d2b03eb17ee31e61a5ec` | E6 `git log -1` |
| HEAD 时间 | 2026-07-14T16:33:04-04:00 | E6 `git log -1 --format` |
| tracked modified | 0 | E6 status + diff |
| stash | 0 | E6 `git stash list` |
| untracked paths | 6,355 | E6 manifest |
| `.venv` untracked | 6,281 | E6 path分类 |
| `.venv` 外 cache/pyc | 65 | E6 path分类 |
| 其余实质 untracked | 9 | E6 path分类 |
| 全 untracked 路径清单 SHA-256 | `ff20e604b9dba988da7c3e1cc7541472c7b1ec64ed1983a4e0984f74c04f9890` | E6：排序后的 `git ls-files --others --exclude-standard` |

折叠 `git status` 全量为：

```text
?? .venv/
?? deploy/__pycache__/
?? deploy/balance_probe.py
?? deploy/status_bot.py
?? deploy/telegram_monitor.conf
?? deploy/tg_alert.py
?? deploy/tg_common.py
?? deploy/tg_daily.py
?? deploy/tg_intel.py
?? reports/
?? tests/__pycache__/
?? tools/__pycache__/
?? tools/account_view.py
```

`reports/` 内实质文件是 `reports/flowback_test_20260709.txt`；9 个非 cache/venv
实质文件为上面 7 个 deploy 文件、该报告和 `tools/account_view.py`（E6）。

当前运行/启用关系（E6；oneshot service 平时可显示 inactive，不能统称 active）：

- `kalshi-status-bot.service` 直接运行 `deploy/status_bot.py`，并动态加载
  `deploy/balance_probe.py`；
- `kalshi-tg-alert.timer/service` 运行 `tg_alert.py`，使用 `balance_probe.py`、
  `tg_common.py`、`telegram_monitor.conf`；
- `kalshi-tg-intel.timer` 与 `kalshi-tg-daily.timer` 分别依赖 `tg_intel.py`、
  `tg_daily.py` 和 `tg_common.py`。

结论：截至 14:02:19Z，tracked source bytes 可由 HEAD/本地 bare origin 重建；
这不等于可部署 runtime 可复现。当前 runtime 不能只由 HEAD 重建，直接阻断项至少
包括上述 `7` 个被一个 active 常驻 bot 和三个 active/enabled timers/oneshot 链路
引用的未跟踪 deploy artifacts。核心 pipeline service 的 PATH 还明确依赖未跟踪
`.venv`，仓库没有完整 dependency lock，环境 provenance 是另一独立缺口。
`tools/account_view.py` 是未被 service/cron/当前进程引用的 operator CLI，flowback
报告只是历史证据。按任务要求未重查已证伪的 143/75 分叉说法。

## 5. 07-12～16 数据数字复核

环境为生产 `/home/ubuntu/hft-bot`，Python 3.12.3 / DuckDB 1.4.5，连接
`:memory:`，`SET threads=2`、`SET memory_limit='4GB'`、
`SET preserve_insertion_order=false`。只读五个路径分区 `date=2026-07-12`～
`date=2026-07-16`，不连 staging DB，也不靠 hive-derived 日期过滤（E7）。

三个 trades 查询只替换 glob 的 subcategory：

```sql
SELECT count(*) AS trade_rows,
       count(DISTINCT event_ticker) AS distinct_event_ticker,
       count(DISTINCT market_ticker) AS distinct_market_ticker
FROM read_csv(
  'work/warehouse/facts/trades/category=Sports/subcategory=Tennis/date=2026-07-1[2-6]/*.csv.gz',
  header=true,
  union_by_name=true,
  types={'market_ticker':'VARCHAR','series_ticker':'VARCHAR',
         'event_ticker':'VARCHAR','category':'VARCHAR','subcategory':'VARCHAR',
         'group':'VARCHAR','trade_id':'VARCHAR','taker_side':'VARCHAR'}
);
```

Baseball/Golf exact glob 分别把 `subcategory=Tennis` 换成
`subcategory=Baseball` / `subcategory=Golf`。Tennis L2 exact query：

```sql
SELECT count(*) AS tennis_l2_rows
FROM read_parquet(
  'work/warehouse/facts/orderbooks_full/category=Sports/subcategory=Tennis/date=2026-07-1[2-6]/*.parquet',
  union_by_name=true
);
```

| 指标 | 精确复算 | event | market | 输入 files / bytes | 旧近似 | 差值 | 相对差 | 耗时 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Tennis trades | 6,338,016 | 2,730 | 5,774 | 5 / 314,542,863 | 6,338,000 | +16 | +0.000252% | 3.523s |
| Baseball trades | 1,894,909 | 2,964 | 7,941 | 5 / 95,063,026 | 1,895,000 | -91 | -0.004802% | 1.344s |
| Golf trades | 447,434 | 178 | 2,978 | 5 / 22,976,508 | 447,000 | +434 | +0.097092% | 0.662s |
| Tennis L2 rows | 84,094,616 | — | — | 5 / 1,320,158,187 | 84,090,000 | +4,616 | +0.005489% | 0.041s |

四项相对差都未超过 `1%`；最大是 Golf 的 `0.097092%`（E7）。上一报告的
633.8万 / 189.5万 / 44.7万 / 8,409万均是可信的四舍五入近似。

## 6. readiness 收据

### 复现

修复前状态由 E8：

| 项目 | 值 | 出处 |
|---|---:|---|
| 当时 core registry | 76 | E8 `core_tools()` |
| lifecycle 缺失 | 15 | E8 `work/lifecycle_status.json`，生成 12:59:10.661Z |
| 其中 NDJSON 已有 pass、latest 缺失 | 14 | E8 NDJSON/latest join |
| 仅聚合进 `make_check`、无独立 suite 的 `test_engine_bench` | 1 | E8 NDJSON/latest join |
| 典型 `test_pricing_lo` NDJSON pass | 12:55:06.230Z | E8 |

根因不是测试没跑，而是收据路径不同：`tests/run_pipeline.sh:24` 只追加 NDJSON；
`tools/lifecycle_check.py:299-308` 只读 latest；`tools/run_tests.py:182-197` 才同时
写两者。

### 第一次登记暴露第二层假身份

第一次执行 `--run-core-tests` 后，14:14:31Z 曾从 latest 显示 `77/77 pass`；独立
复核证明这个绿色不能保留：`tools/run_tests.py` 只执行 `cmd`，不会执行仅供 UI
展示的 `args_template`。`37` 个 pytest registry key 的 `cmd` 都只是
`./tests/run_pytest.sh`，所以每个 key 重复跑了全仓 pytest，而没有跑自己的文件。
这 `37` 项旧耗时为 `17.743～20.075s`，`test_l2_gap_check` 是 `19.027s` 且 receipt
为 `passed=0/failed=0`（E8）。因此第一次“77/77”是 suite identity 假绿。

### 修复执行身份并重新登记

提交 `6d518cf fix lifecycle pytest receipt identity` 将 `37` 个固定 target 移进
真实 `cmd`，保持 `args_template` 永不执行，并在 registry/console tests 写死：
每个 autorun pytest test 必须恰有一个存在的 `tests/test_*.py` target。四个入口
（CLI 单项、`--all`、lifecycle、dashboard）都共用 `run_tool()`，因此同时修复。

修复后 `test_l2_gap_check` 单项 receipt 为 `173ms`，log 精确为
`17 passed in 0.03s`。重新执行全部 `77` 个 core registry key 后：

| 项目 | 值 | 出处 |
|---|---:|---|
| core registry | 77 | E8 |
| pytest targeted keys | 37 | E8 |
| core pass / fail | 76 / 1 | E8 latest + lifecycle |
| pytest pass / fail | 36 / 1 | E8 |
| targeted pytest duration range | 160～9,899ms | E8 |
| `test_kalshi_golden` | fail；202ms | E8，focused log 为 `1 skipped in 0.03s` |

这一个红灯是真阻断，不再用“全仓其他测试通过”掩盖：golden module 的 V12 gate
拒绝在过期/漂移的官方 spec receipt 上执行。随后只读拉取 Kalshi 官方文档到系统
临时目录，`fetch_errors=0`、`accepted_baseline=false`；OpenAPI 从 baseline SHA
`557683c9…55b4cbe` 变为 `71fbc7d7…59059e`（`307,951` bytes），AsyncAPI 从
`00858d5a…7d421` 变为 `e8610537…1372f`（`142,396` bytes），另有 `3` 个 info
级 hash change（E8）。未审阅/接受新 baseline，所以 core stage 与 overall
均为 `LIFECYCLE FAIL`。任务要求的“正规登记”已做到，但“确认转 pass”被真实
spec-drift gate 证伪；强行改 token 或接受 baseline 都会制造绿灯，未做。

剩余收据缺陷只记账：`run_tests.record()` 仍不绑定 commit、tree/dirty diff 或
config hash（`tools/run_tests.py:174-197`）；按任务边界未扩建这些 schema 字段。

## 收口状态

| 项 | 状态 | 结论 |
|---|---|---|
| 1 | PASS / FIXED | 假绿双向复现，永久测试，独立提交 |
| 2 | PASS / FIXED；数据有隔离项 | detector 不再让拒绝行污染总数；07-14 `l2_00` 必须排除 |
| 3 | PARTIAL | 静态防护已在生产；capture growth measured；checkpoint advance unproven；按立即交付指令未等 02:10Z seal 终证 |
| 4 | FAIL（runtime 可重建性） | tracked source clean；runtime 依赖 7 个 untracked deploy artifacts + untracked `.venv` provenance |
| 5 | PASS | 四个旧近似全部在 1% 内 |
| 6 | INFRA FIXED / CORE FAIL | 37 个 pytest identity 已修；正规收据为 76 pass / 1 spec-drift-gated fail |

最终代码验证于 `2026-07-17T15:02:18Z` 完成：外层 `make check` 全绿；随后
`tests/run_pipeline.sh` 全部 suite 退出 0 并打印 `PIPELINE PASS`，其中
`test_l2_gap_check` 为 pass、`198ms`（E8 / `work/test_results.ndjson`）。pipeline
按进程退出码把 module-level skip 的 `test_kalshi_golden` 记为 pass；readiness 的
registry pass-token 路径仍把它记为 fail，所以这里的 `PIPELINE PASS` 不覆盖
15:02:42Z 的 core/spec 红灯。

这批结果支持 Research 读取链完成后立即做 deepresearch V3 的数据分析，但不支持
把 07-14 `l2_00` 当可裁决窗口，也不支持把 core tests 写成全绿或据此 live-ready。全程未
触碰采集控制面、未发送订单、未提交工作树中其他人的改动。
