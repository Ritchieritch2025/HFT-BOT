# PIPE-W03 根因报告:2026-07-10 hour-13 raw 为何从未被 ingest 发现

**一行裁决:❌ 轮转命名(rotation-naming)嫌疑不成立 —— glob 从 2026-07-06 起就
覆盖轮转分片;真正的根因是旧 ingest 守护进程在启动时裸连 DuckDB(无锁重试),
被长寿命持锁进程连环秒杀,从 2026-07-10 ~13:00Z 起整条入库线停摆 ~21 小时,
扫描器不是"漏看了这个文件",而是"根本没活到去看"。当天 57% 的数据
(20.78M 行)缺失,旧出口照发绿灯 —— D2 教科书案例。**

写于 2026-07-12(PIPE-W03 单工程会话,worktree 分支 `pipe-w03`)。
所有远端读数均为只读命令(`tools/ec2_health.sh` + ls/cat/stat),零生产改动。

---

## 1. 事实链(每条带出处)

### 1.1 被点名的文件

- `work/raw/date=2026-07-10/firehose_13.ndjson`,268,435,346 字节,
  首封拒绝语:`INGEST CATCH-UP FAIL 2026-07-10: ingest checkpoint behind raw:
  .../firehose_13.ndjson checkpoint=None size=268435346`
  (EC2 `work/live/export.log` 第 1633 行,2026-07-12 只读抓取)。
- 该文件 13:00:00Z 创建、13:13:55Z 写满关闭(268MB ≈ WsRecorder 轮转阈值;
  seal 证据 `work/warehouse/seals/date=2026-07-10.json` 记录其
  mtime_ns=1783689235526230399 = 13:13:55Z)。之后写手轮转到
  `firehose_13.ndjson.1/.2/.3/.4`(同 seal 记录)。
- `checkpoint=None` 语义(`tools/export_day.py` verify_raw_caught_up,
  按 abspath 查 `stg.checkpoint` 表):该文件从建立到 07-11 首封检查,
  **一个字节都没进过 staging**。

### 1.2 轮转命名嫌疑:排除

- 部署前生产代码(EC2 checkout @ `a011fab`,见 merge 提交 `f0e36ab` 的说明)
  的扫描器 `git show a011fab:tools/ingest.py`,第 395-404 行:
  `raw_files_to_scan` 对"昨天+今天"两个日目录 glob `*.ndjson*`,注释原文:
  *"\*.ndjson\* also matches WsRecorder rotation shards (base.ndjson.1, .2,
  ...)"*。与当前(W02 后)代码 `tools/ingest.py:679-688`(改动前)**逐字节相同**。
- 该 glob 早于 2026-07-06 rotation-shard 事故修复就位
  (首见 `ac2d2ad`,2026-07-06 batch rescue commit)。
- 也就是说:**基名 `firehose_13.ndjson` 和分片 `.1` 在 glob 意义上都被覆盖**。
  轮转命名是红鲱鱼。且 hour-13 的 `.1/.2/.3/.4` 分片当时同样没有 checkpoint
  ——不是"基文件被轮转藏起来",是整个时段之后什么都没入库(见 1.4)。

### 1.3 真正的击杀机制(有直接击杀现场)

旧 ingest(`git show a011fab:tools/ingest.py`)`main()` 第 423 行:

```python
con = duckdb.connect(staging)     # 裸连,无重试;在 "classes loaded" 横幅之前
```

`connect_with_retry`(第 376 行)当时**只**接在 loop 内的重连点(第 434 行),
不在启动路径。任何长寿命进程持有 staging.duckdb 写锁时,supervisor 看门狗
每 60s 拉起的守护进程都会在毫秒内死于第 423 行 —— 连扫描循环都没进过,
`raw_files_to_scan` 一次都没执行。

EC2 `work/live/ingest.log`(只读抓取,共 659 行)里的击杀记录:

- 62 个 crash traceback,其中 **21 个是旧代码 `ingest.py line 423`**:
  `_duckdb.IOException: ... Conflicting lock is held in /usr/bin/python3.12
  (PID 90986)`(×20)与 `PID 90988`(×1)—— 同一个长寿命 python 进程连续
  持锁,守护进程反复扑锁反复死。
- 另有 17 个旧代码 line-423 击杀,持锁者 PID 1718xx(07-11 ~08:3xZ
  HOTFIX-01 重启后的又一轮持锁),以及 24 个**新代码** line-710
  (connect_with_retry 150s 重试耗尽)击杀,持锁者 PID 3175
  (操作员 10:45Z stdin 补灌 —— 即 BACKLOG B4 现场)。
- `work/live/supervisor.out.log`:共 96 条 "ingest daemon started";
  supervisor `start pid=90953` 之后连续 ~25 条拉起记录,一个都没活下来
  (对应 ingest.log 里只有 traceback、没有 "classes loaded" 横幅 ——
  旧代码死在横幅打印之前,两份日志互证)。

持锁者身份:PID 90986/90988 是 pid=90953 supervisor 启动后立刻派生的
python 子进程,与旧 supervisor 首循环的出口/二次 --force sweep/研究链
(mm_scan → mm_backtest → mm_calibrate,当时仍启用,直到 07-11 08:27Z 的
PIPE-HOTFIX-01 `a011fab` 才注释掉)一致;BACKLOG B5 记录了研究链把 16G
盒子顶到 15.2G、长时间不退的行为。**诚实边界**:这些日志不带时间戳,
journalctl 不在本次只读授权范围内,无法从幸存日志确证 90986 具体是
mm_* 还是 export_day --force;但两者都是 supervisor 自己的长持锁子进程,
不影响结论 —— 设计缺陷(启动裸连 + 停摆不可见)才是把"一把锁"放大成
"21 小时静默丢数"的原因。

### 1.4 停摆边界与爆炸半径(定量)

- seal 证据里 `firehose_12.ndjson.3` 的 mtime 恰为 13:00:00Z;首封检查按
  排序逐个核对,**00-12 点全部文件(含所有轮转分片)checkpoint 与字节数
  精确相等**,到 hour-13 基文件才炸 —— 而部署后新守护 10:20-10:24 只跑了
  几分钟、不可能补完 13 小时,故 00-12 的 checkpoint 只能是 07-10 当天
  旧守护**活着时**写下的。⇒ 入库停摆点 ≈ 13:00Z。
- 旧系统 07-11 00:00Z 对 07-10 的出口:`EXPORT PASS 2026-07-10: 291 file(s)`
  (export.log:868),全天合计 **15,496,441 行**;补灌+封印后的最终出口:
  `EXPORT PASS 2026-07-10: 302 file(s)`,**36,277,704 行**(export.log
  1652-1972 两段逐分区对比,本会话解析)。即旧绿灯下缺了
  **20,781,263 行(57.3%)、302 个分区中的 296 个有行数差** —— 恰为
  13:00Z 之后的大半天。绿灯与事实的差距就是 D2 条款存在的理由。
- 为什么拒绝语只点名一个文件:W02 的 verify_raw_caught_up 按排序在**第一个**
  未达标文件处抛错(改动前 `tools/export_day.py:213-216`),hour-13 基文件
  在缺失集中排序最前。`.1/.2/.3/.4` 与 14-23 点同样缺失,但没被报出来 ——
  爆炸半径被系统性低报(本 W 已修,见 §3)。

### 1.5 时间线(可证部分)

| 时刻 (UTC) | 事件 | 出处 |
|---|---|---|
| 07-10 ~13:00 | 入库最后推进点(firehose_12.ndjson.3 mtime 13:00:00Z,checkpoint 精确) | seal 证据 |
| 07-10 13:00-13:14 | firehose_13.ndjson 写满 268MB 关闭,随后 .1-.4 分片 | seal 证据 mtime_ns |
| 07-10 13:00 起 | 守护进程 line-423 连环死(持锁 90986/90988),入库零推进 | ingest.log / supervisor.out.log |
| 07-11 00:00 | 旧出口对 07-10 发绿灯 "291 files"(实缺 57%) | export.log:868 |
| 07-11 08:27 | PIPE-HOTFIX-01(a011fab)禁研究链 | git log |
| 07-11 10:19 | W02+TL1 部署重启;新门 check-caught-up 首封拒绝,点名 hour-13 | PIPE-R001 部署报告、export.log:1633 |
| 07-11 10:45-12:59 | 操作员补灌(PID 3175);11:00Z 链误删其 pause(=B4) | 部署报告、BACKLOG B4 |
| 07-11 13:19:48 | 07-10 正式封印(151 raw 文件全部 checkpoint 精确) | seals/date=2026-07-10.json |

---

## 2. 当前(已部署)代码是否仍有该 bug 的残余变体?

**有,三处;本 W 全部修掉(仓库内,待下个运维窗部署):**

1. **扫描窗口逐出(真残余,线上有活例)**:`raw_files_to_scan` 只扫
   "昨天+今天"。一个停摆日一旦超过 1 天没被补完,其 raw 就永远离开扫描
   窗口 —— 正是本次事故再晚两天部署就会发生的事。**活例**:EC2 上
   `date=2026-07-09` 的 18-23 点 41 个文件至今未封(prune 的
   `raw_retention_alert.json` 全部标 `day_unsealed`),已在窗口之外,
   改动前没有任何机制会再去入库它们。
   **修复**:扫描集 = 昨天+今天 ∪ 所有在盘且**未封印**的日目录
   (`tools/ingest.py raw_files_to_scan` + `warehouse_common.day_sealed`,
   与 prune_raw 共用同一封印判定)。回归测试构造了"3 天前的未封日 +
   轮转分片"场景:旧窗口(测试内复刻)漏掉、新扫描器抓到并 checkpoint
   到字节精确(`test_unsealed_old_day_rotated_raw_stays_discoverable`)。
2. **首个违例即抛错 → 低报**:已改为收集**全部**落后/未发现文件后一次性
   报出(`verify_raw_caught_up`;`test_caught_up_failure_reports_every_behind_file`)。
3. **封印证据无分通道台账**:seal 现在附加 `discovery_completeness` ——
   按文件族前缀(今天 `firehose_`,前向兼容规划中的 `l2_`)记录
   在盘小时/分片数 vs 已发现数、以及交易所日 24 小时中盘上**完全没有文件**
   的小时列表(采集空洞也上台账)。纯增量证据,不弱化任何既有门
   (`test_seal_evidence_records_per_family_discovery`、
   `test_discovery_completeness_is_per_family_and_shard_aware`)。

**W02 已修、本次确认有效**:启动路径 `connect_with_retry`(150s/轮 +
看门狗重拉 ≈ 无限耐心等锁)—— 07-11 当晚在生产对 PID 3175 场景自证
(ingest.log 24 次重试耗尽后最终接管;部署报告已记)。

**仍开放(诚实清单,不属本 W)**:
- process_file 内单文件异常(OSError/MemoryError 等)仍会杀掉整个扫描
  循环 → 看门狗重拉 → 同型饥饿(触发器不同)。现有缓解:封印门 + 03:00
  告警最迟一天内暴露;彻底修复留给后续 W。
- 追赶期无界尾部物化(12.9G RSS)与研究链内存围栏 = BACKLOG B5(未动)。
- `2026-07-09` 本身:旧门时代出口的完整性**未验证**;本 W 部署后扫描器会
  自动补入其缺失字节,但封印链只封"昨天",07-09 没有自动封印路径 ——
  是否补封(以及用哪条路径)是操作员决定项。

---

## 3. 本 W 的对应改动(便于 diff 审计)

- `tools/ingest.py` — raw_files_to_scan:未封印日永久可发现。
- `tools/warehouse_common.py` — 新增 `day_sealed()`(与 prune_raw 共用)。
- `tools/prune_raw.py` — `_sealed` 改为调共用判定(行为不变)。
- `tools/export_day.py` — verify_raw_caught_up 全量报告;
  `discovery_completeness()` 新增并写入 seal 证据 + `--check-caught-up`
  控制台输出。
- `tools/pipeline_supervisor.sh` — B4:export_pause 所有权令牌
  (`seal_chain pid=<链pid>`);外来 pause ⇒ 拒开出口窗且**不删不覆盖不重启
  ingest**;只删自己写的 pause;死链遗留的 pause 大声回收。
- `docs/BACKLOG.md` — B4 标记 FIXED 并指向实现与测试。
- `tests/test_pipeline_contract.py` — 上述全部行为的 7 个新测试
  (含把旧扫描器窗口复刻进测试以证明"旧漏新抓")。

## 4. 附:Stage-1 gate (a) 后半 —— prune 复跑核验(只读,2026-07-12 00:2xZ)

- `bash tools/ec2_health.sh`:SERVICE=active,INGEST=ALIVE(pid 857),
  SEAL: date=2026-07-10.json,SEAL_ALARM=NONE,磁盘 42% 用量。
- `work/live/prune_raw.log`:07-10 封印后 13 次整点运行
  `PRUNE PASS: deleted=0 retained_overdue=41` —— 07-10 已封但**未满
  retention-days=2**,不删是正确行为(与释放令预期一致)。
- **2026-07-12T00:00:01Z 起 07-10 满 2 天**(cutoff=2026-07-10):
  `PRUNE PASS: deleted=123 retained_overdue=54` —— 已封印的 07-10 raw
  123 个文件被正确删除;`raw_retention_alert.json` 逐文件给出保留原因:
  41 × `day_unsealed`(2026-07-09 遗留未封日,受保护 ✅)+
  13 × `cross_day_prev_unsealed`(07-10 的 00/01 点文件,因前一日 07-09
  未封而保留 —— 跨日收据依赖,fail-closed ✅)。
- 结论:**prune 已复跑且首次真实删除行为完全符合封印门控设计**;
  超期未删文件全部有理由、有台账,无静默跳过(D2 ✅)。
