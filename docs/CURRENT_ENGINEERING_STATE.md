# CURRENT_ENGINEERING_STATE — 工程状态参考快照(非规则)

- created: 2026-07-14(依据 `docs/ENGINEERING_STATE_REVIEW_2026-07-14.md`
  评审 + 本仓库逐项核验)
- **性质(操作员裁定 2026-07-14):本文件是描述性参考快照,不是规则,
  不对任何 agent/会话新增任何义务或流程。** 项目的约束权威不变,仍然
  只有:GUARDRAILS(宪法)、MASTER_SEQUENCE(工程队列)、
  PLAN_SPORTS_TRADING_DECISIONS(策略裁决)。本文件可能过期;与档案
  证据冲突时以证据为准。更新它是自愿的好习惯,不是门槛。
- 状态词汇(仅为读图方便):**BUILT**(代码在、测过)/ **DEPLOYED**
  (真的在 EC2 上跑)/ **ACCEPTED**(有生产数据+验收证据)/
  **BLOCKED**(整条链还不能用)。"已写代码"不等于"已完成"。
- 分支注记:EC2 侧事实(部署/验收)以生产谱系分支与 EC2 档案为准,本文件
  在策略分支维护,标注证据出处。

## 0. 生产分支现状(观察 + 建议,非指定)

- 观察:最新生产能力(W03/W05/W06/W07/HOTFIX-02/新 seal 行为)都在
  `codex/pipeline-recovery-hardening` 谱系上;本策略分支
  (`plan-sports-market-dynamics-v2`)的 supervisor 是旧版。
- 建议(非规则):下次生产部署从 pipeline-recovery 谱系出发,避免回滚
  生产能力;并择机建统一集成 tip(生产谱系 ← 合并 W05-UI@34e3a38 与
  策略分支文档)跑全量测试。是否采纳、何时做,由操作员按需决定。

## 1. 子系统台账

| 子系统 | 状态 | 证据/出处 |
|---|---|---|
| 全市场 L1+trades 采集(firehose) | **DEPLOYED + ACCEPTED** | 07-12 封印 1.09 亿行;TL1 阶梯列;W-A4 割接后 EC2 连续运行 |
| 定向 L2(约 50 市场/小时,W06 Stage 1) | **DEPLOYED + ACCEPTED**(Stage 1) | Stage 0 探针 694.5 msg/s/0 错(w06-stage1 日志);Stage 2(~200 市场)NOT BUILT;B10 selector fail-closed 缺陷开放 |
| RFQ 广播采集(W07) | **DEPLOYED + ACCEPTED**(仅 raw) | kalshi-rfq-capture.service;rfq_<HH>+receipts 双文件族;研究表(rfq_requests 等)NOT BUILT |
| Ingest + checkpoint(含 W03 修复、RFQ fast-path) | **DEPLOYED + ACCEPTED** | PIPE-W03 收据;ADDENDUM 7 fast-path |
| 日封印链(export→manifest→seal→verify→gap) | **DEPLOYED + ACCEPTED** | 07-12 seal verify PASS 18:31Z(07-13);**B11 根因未完全复证**(锁竞争解释与 supervisor pause 代码不吻合,需 PID journal+部署 SHA 复证) |
| S3 raw 保险库 + 同步定时器 | **DEPLOYED** | W-A5;closed-hour 排除、无 --delete |
| **W05 研究 release(数据桥)** | **BUILT + BLOCKED(B12)** | 发布器/CLI/hook 在生产谱系;缺 vaultWriter `s3:GetObject(Version)` 回读权限 ⇒ fail-closed;桥上仅 07-11 QUARANTINED_LEGACY;`DATA_PLANE_ACCEPTED` 不存在 |
| W09 云研究机 | **NOT BUILT** | 仅注册令(PIPE-W05-SPEC ADDENDUM 5);无实例/spend/IAM/runner |
| Research Workbench / Event Intelligence | **BUILT**(sandbox 级) | 读旧本地数据(07-06~08,21 episodes);W05-aware UI 在 `pipe-w05-ui-data-root@34e3a38` **未合并** |
| mm_sandbox 做市回放器 | **BUILT — DIAGNOSTIC only** | 无真实 L2 校准队列、tape 缺完整 recv/seq 证明 ⇒ 不给经济 go/no-go |
| C++ 引擎性能件(decoder/book/queues/checksum) | **BUILT**(kernel-bench 级) | 缺全路径 replay/p99.9/整封印日 checksum 证明;engine_proof.cpp 未跟踪 |
| K-track 风控(K1~K5:只读账户/panic/预留账本/规则引擎/对账) | **BUILT + 审计通过** | 2026-07-10 日志;W-K6 实盘演练待操作员排期;**未接入统一执行链** |
| P-track 定价数学(lo/fair/quote + 黄金场景) | **BUILT + 审计通过** | tools/pricing/;Group C 校准等 recv 数据 |
| tradingd(World A,可下单、REST 轮询) | **BUILT — Q5 不合规,替换对象** | ARCHITECTURE_REVIEW"两个世界";部分路径绕开统一 executor |
| World A/B merge / 统一做市引擎 | **NOT STARTED** | Phase 2 开工件;缺口清单见评审 §五 |
| STP 治理程序 | P00 **ACCEPTED**(审计 PASS);P01+ **BLOCKED**(等 release) | PLAN_SPORTS_TRADING_STATE |
| AutoResearch(SPORTS-AUTORESEARCH-01) | **BLOCKED**(GATE A 实测 FAIL) | 草案已审计 ACCEPT-WITH-FINDINGS,4 项 P1 待一次性文本修补 |

## 2. L1×L2 对齐状态(2026-07-14 裁定口径)

**结构上可精准配对,但从未正式验收——"能对齐"目前是设计保证,不是验收事实。**

- 已有的对齐机制:两条流落在**同一台采集机**,共享 TL1 时间戳阶梯
  (`local_recv_ts_us` = 可交易决策时钟,warehouse_schema §ladder);
  `orderbooks_full` 带 `ws_sid/ws_seq`(每流单调序列号),在线 SidStream
  断档即整流作废+流内快照重锚,离线 `l2_gap_check.py` 封印后复核
  (PIPE-W06 spec §5)。⇒ L1×L2 用 as-of join 按接收时钟配对,是有依据的。
- 三个诚实边界:①配对只存在于被 L2 选中的约 50 市场;②精度 = 我们的
  接收时钟(亚毫秒),两条独立 WS 连接在交易所侧的真实先后无法完美复原,
  毫秒内交错以我方接收顺序为准;③**缺一份正式的跨通道对齐验收报告**
  (同市场 L2 顶档重构 vs L1 报文逐段核对)。
- 工件缺口:`L1×L2 ALIGNMENT_REPORT`(W05 验收后的第一个小研究件)+
  L2→回测器 tape adapter。在此之前,任何"精确队列/微观结构"结论只能标
  DIAGNOSTIC。

## 3. 退役参考清单(怎么"安全地去掉旧零件")

建议做法(引用既有宪法 P6,不新增规则):**attic 不 rm**(移入 attic/
或打 SUPERSEDED 横幅,不直接删除)、一次退役一件、测试绿 +
`check_registry` 过。原始数据/seal/S3 永远不在退役范围。下表是参考
清单,每项动不动、何时动,由操作员决定。

| 零件 | 现状 | 先决条件 | 动作 |
|---|---|---|---|
| `apps/live_e2e.cpp`(未注册可下单,C-13) | 悬置 | 操作员裁决 | **建议退役入 attic**(register-or-retire 二选一) |
| `preflight --order` 模式(注册类别错配) | 在用 | 无 | 拆分注册为 live_order 条目(§26) |
| `deploy/bootstrap.sh`、`tradingd.service`、`tradingd.env.example` | LEGACY 已标注 | World A/B merge 落地 | merge 后 attic |
| tradingd REST 轮询策略路径(World A) | 替换对象 | merge 落地+shadow 绿 | 替换后 attic,不先删 |
| Mac launchd 管道(回退宿主) | dormant | S3 恢复测试通过 + EC2 连续 N 干净日(操作员定 N) | 达标后卸载入 attic |
| Mac 本地旧 warehouse | dormant/过期 | W05 验收 + Workbench 切到 release 数据根 | 只读封存,不删 |
| 旧 prompt V2/V2.1、被取代的计划 | 历史 | 无 | 保留 + SUPERSEDED 横幅,**不删**(审计链需要) |
| `mm_backtest.py` | 诊断级(9 缺陷在案) | W-FS1 | 保留 + 横幅"不作 go/no-go" |
| sandbox 嵌套克隆(w05-recovery-fix/、handoff patches) | 未跟踪,污染搜索 | 其补丁全部进入生产谱系 | 确认合入后整目录移出仓库树归档 |
| `__pycache__`、`outputs/`、`.claude/` | 未跟踪噪音 | 无 | gitignore 收口(注意 .gitignore 写权限走正常 W) |

## 4. 操作员待批清单(按解锁价值排序)

1. **B12 IAM**:vaultWriter 加 `s3:GetObject`+`s3:GetObjectVersion` on
   research/*(约 5 分钟,解锁整条研究线)。
2. 生产分支指定追认(本文件 §0 一句话)。
3. AUTORESEARCH-01 的 4 项 P1 文本修补批复(一次改完,不再开审计循环)。
4. C-13(live_e2e 退役)、OQ-1(费率追认)、C-1/C-2(小追认)。
5. W-K6 实盘演练排期;W09 spend gate(等 W05 验收后)。
6. B11 根因复证 + B9/B10 修复排期(W06 Stage 2 前置)。
