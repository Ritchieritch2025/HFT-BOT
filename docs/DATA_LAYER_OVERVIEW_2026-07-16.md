# 数据层全景与策略构建缺口清单 — 2026-07-16

状态: 评估报告（非计划、非决定）。三路独立代码/文档勘查的汇总:
①采集→入库→封存全链路 ②S3 布局与重复审计 ③研究端数据消费。
所有数字均标注出处；未实测 S3 体积（本机无 aws CLI/凭证）。

## 一行裁决

⚠️ 数据层足以支撑"描述性研究"，但距离"能给策略下 go/no-go 裁决"还差
四个硬缺口（费率未 ratify、封存日不足、L2 覆盖极少、无结算数据）；
S3 research 前缀的"整份复制"是**有意设计**（不可变研究快照），不是 bug，
当前成本很小，但零生命周期管理会让它线性膨胀。

## S3 重复问题（操作员主诉）的裁决

**确认存在重复，但性质是"跨前缀的结构性复制"，不是"每次跑都重传一遍"。**

- 主同步 `deploy/ec2_s3_sync.sh` 用 `aws s3 sync`（增量，按大小/时间跳过
  已传文件），每小时 raw + reports，每天 warehouse。无重传 bug。
- research 发布器（`sandbox/w05-recovery-fix/tools/research_release.py`，
  尚未合入主干但已运营 6 个 release，见 SESSION_LOG:296）按 W05 规格
  （PIPE-W05-SPEC-2026-07-12.md:31）**故意**把已封存日的
  facts/dim/catalog/manifest 逐字节复制到
  `s3://kalshi-vault-ritcardo/research/releases/<release_id>/`——
  而这些字节本来就在 `ec2/warehouse/` 前缀里。目的: 不可变、
  version-pinned、研究机只读。
- 每次 correction/gap 证据/RFQ 开关翻转都会生成**新 release_id = 又一整份
  复制**，旧的按 D1 规则永不删除，桶还开了 versioning。
- RFQ 开关 (`RESEARCH_INCLUDE_RFQ=1`) 是最纯的复制路径: 从 `ec2/raw` 下载
  再回传到 `research/…/raw_rfq/`，代码自己标注 +$21–23/月复利成本，
  默认关闭（research_release.py:76-81）。
- **零生命周期规则、全 STANDARD 存储级**（$0.023/GB-mo），全桶 +~27GB/日
  （status_bot.py:267）。6 个 release 的重复量目前绝对值小，但随封存日
  线性增长。

可选省钱项（均不违反 D1 不删除规则，属操作员成本决定，仅列出）:
a) 研究机直接读 `ec2/warehouse/`（W09 读路径已支持），停掉 research 前缀
   的归档复制；b) 给 `research/` 和老 `ec2/raw` 加 IA/Glacier 生命周期
   转档。精确体积需在 EC2 上跑
   `aws s3 ls s3://kalshi-vault-ritcardo/{ec2/warehouse/facts,research}/ --recursive --summarize` 对比。

## 数据层分层图（现状）

1. **采集（EC2, C++ ws_shadow + supervisor）**: firehose 全市场 L1+trade;
   L2 仅定向 ~50 市场/小时（07-06 实测有效全深度仅 4 个）; RFQ 单独连接
   仅存 raw; 生命周期/结算帧未采集。小时轮转 NDJSON，raw 本地留 2 天。
2. **入库（ingest.py → staging.duckdb）**: change-only + 心跳压缩，
   E4 定点类型（价 INT32×1e4）。
3. **封存（export_day.py）**: 午夜导出 zstd-15 parquet + seal 链，
   `verify-seal` PASS 是唯一权威完成信号。
4. **研究消费两平面**: 本地 warehouse.load()（staging∪archive）
   vs 密封 release 平面（autoresearch，只认 VERIFIED release，不读 S3）。
5. **时钟阶梯（W-TL1）**: 可交易决策钟 = `local_recv_ts_us`;
   `ts_utc` 多数行是交易所时间——拿它当回放钟 = 前视偏差。

## 影响策略构建的问题（按严重度排序）

1. **✅ 已解除 2026-07-16 — 费率 ratify 完成（D-4）** — 原状态: OQ-1
   悬案导致 fees.verified=false，一切悲观轨 NetPnL 与 go/no-go 被闸死。
   本日以多来源证据链 ratify（操作员委托，D-4 in
   PLAN_SPORTS_TRADING_DECISIONS.md），verified 已翻 true，哨兵测试同步
   更新；WP-07/WP-09 rerun due。残余风险与证据细节见 D-4。
2. **❌ 封存日远不够 20** — deep03 Phase B 要 ≥20 独立封存日，现正典
   发布日仅 07-12/13（07-15 封存未发布）。且封存链本身反复出事:
   07-09 至今未封存、B11 写锁竞态未修、W-A 修复已建成**未部署**
   （PIPE_DEBT_PAYDOWN_PLAN:14-22）——面板增长速度本身有风险。
3. **❌ L2 深度覆盖极少且从未验收对齐** — firehose 不订 orderbook_delta，
   W06 Stage-2（~200 市场）冻结未建; deep02 的头号候选（88–93¢ 拉锯区
   双边报价）明确需要"L2 深度蒸发信号"，deep01 时仅 1 个有效 L2 日。
   另: B14 采集端仍在给 L2 帧打坏 recv_mono_ns（毫秒内排序丢失）。
4. **❌ 无结算/生命周期数据** — mm_backtest 不建模结算（按日末 mid 记账）;
   85–99¢"卖确定性"候选需要持有到结算的 PnL 口径; 仓库里没有权威
   开赛时间（kickoff ≠ close_time，来源 TBD）——"赛前"边界只能推断。
5. **⚠️ 时钟陷阱与覆盖** — `ts_utc` 是默认查询键但多数行为交易所时间
   （前视偏差脚枪）; 描述性工具（mm_research/mm_calibrate/
   maker_edge_pilot）全部键在 ts_utc 上，不能产出 recv 钟的裁决数字;
   A2 门要求 recv 覆盖 ≥95%。
6. **⚠️ 历史数据完整性有既往事故** — B17: 1470 万条未来日期脏行已清但
   根因未钉死; 45.9k 成交丢失先例; 每天 00:00Z（~78s）与 02:00Z
   （~8 分钟）两个采集微缺口，恰好压在日界——赛前窗口构建要避开。
7. **⚠️ 回放吞吐 0.9 MB/s** — 20 天全量回放约 30 小时，参数扫描不可行;
   B9 批量通道仅 PROPOSED（PLAN_B9_BATCH_INGEST_2026-07-16.md）。
8. **⚠️ 延迟参数是保守占位值** — backtest_latency.yaml 的
   processing_chain_p99 / signed_post_rtt_p99 未实测，需 5/50/500ms
   敏感度矩阵替代（DESIGN_BACKTEST_GAP_MAP:52-55）。
9. **⚠️ 工具泛化债** — build_segments/maker_edge_pilot 的分层语义是
   Tennis 专用; 三向 overround 研究缺 catalog 派生的 match-winner
   家族图; RFQ broadcast 无成交/报价接受信息（PnL 结构性不可得）。
10. **⚠️ 分支割裂** — 生产数据平面在 codex/pipeline-recovery-hardening
    谱系，本策略分支本地数据停在 07-06..09、无 seal 文件; 本地做的研究
    看不到生产 seal/L2/RFQ，须经 release 拉取。

## 对策略主线（赛前市场动力学）的最短路径含义

deep03 的两个前置条件（≥20 封存日 + 费率 ratify）与上表 #1/#2 完全重合;
#3(L2) 和 #4(结算) 决定 deep02 两个头号候选能否从 DIAGNOSTIC 升级。
即: **先修费率 ratify（小）→ 部署 W-A 保住封存链（面板增长）→ L2
Stage-2 与结算/生命周期采集是候选策略的数据先决**，回放吞吐(B9)在
面板到 20 日前不构成阻塞。此段为分析推论，非排程决定——排程归
MASTER_SEQUENCE / 操作员。
