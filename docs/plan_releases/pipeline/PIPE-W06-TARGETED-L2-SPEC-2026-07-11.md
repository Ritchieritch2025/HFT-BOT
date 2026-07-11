# PIPE-W06 — 定向 L2(全深度订单簿)采集规格 v1.0(已批)

**一行裁决:✅ 操作员已批准(2026-07-11)—— Stage 0 今日执行;三阶段路线与
universe 构成核准;Stage 1 附加三条硬门(见批准收据)。**

状态:APPROVED — 批准原文逐字存于
`docs/plan_releases/pipeline/PIPE-W06-APPROVAL-2026-07-11.md`(v1.0 @ 46bad42,
SHA-256 e00782c2…)。Stage 1 release 前置条件(操作员附加):
(a) 07-10 封印落地且 prune_raw 恢复;(b) W03 轮转命名结论吸收进实现;
(c) probe 实测替换 30–100× 括号。授权来源:PIPE-R001 收据
"PIPE-W06: targeted L2/lifecycle collection with bounded rollout"。
实现与部署各需独立 W + 独立审计 + 操作员逐阶段放行。

---

## 1. 要解决什么

现在 7×24 采的是全市场 ticker+trade(相当于"每个市场只看最优一档、且被交易所
合并压缩过")。策略主线 D-1(赛前市场动力学价差捕获)要看 **L2 全深度**:每个
价位上挂了多少量、谁在加谁在撤。交易所规定 L2 频道(`orderbook_delta`)必须
**点名市场**订阅、没有全市场模式(PLAN_DEPTH_EXPANSION §1 L2),所以只能"定向":
挑对市场,订全深度。

## 2. 订什么(universe)

- **体育四大类 = Tennis / Baseball(MLB) / Soccer / Basketball(WNBA)。**
  出处:07-08 全市场排名表 `work/mm/depth_target_2026-07-08.csv`(20,944 个
  市场)按 Sports 子类聚合当日成交笔数:Tennis 1,121,009 / Baseball 761,193 /
  Soccer 311,881 / Basketball 173,079;第五名 Esports 163,501 起断档,之后
  Cricket 41,377 陡降。四大类覆盖当日体育成交的 ~84%。
- **对照组(约 15% 订阅预算)**:Esports + Golf(体育域内、主线外)+
  Crypto BTC 15 分钟(非体育微结构基线)。用途:假设台账 H-OP-1/H-OP-2 的
  band×sport×phase 检验需要主线外参照系,避免"只看四大类"自证。
- **选择器(新工具 l2_targets)**:每小时从最新目录快照 + 近期活跃度选
  top-N(每类配额),偏好赛前窗口(距开赛 ≤24h,开赛/结算时间来自
  market_lifecycle_v2),原子写 `work/live/l2_targets.csv`。
  fail-closed:文件缺失/过期 ⇒ L2 层不启动,firehose 毫不受影响。

## 3. 怎么订(架构 = PLAN_DEPTH_EXPANSION §4 Option A,现在选定)

**第二个独立 ws_shadow 实例、独立 WS 连接**,订 `orderbook_delta`(点名市场)
+ `market_lifecycle_v2`(全局、免点名、量极小,给 Q6 到期感知与赛前窗口定义)。

- 与生产 firehose **完全隔离**(P4):独立进程、独立连接、独立原始日志族
  `work/raw/date=<D>/l2_<HH>.ndjson`,每小时随 supervisor 换文件重启
  (副产品:每小时重订阅 = 每小时全簿快照重锚定,数据质量更好)。
- 连接数风险:交易所默认 tier 允许 200 条 WS 连接、官方支持多连接分片
  (docs/kalshi_ws_protocol.md I12)——第二条连接是低风险,probe 阶段顺带实测。
- ws_shadow 是只读 harness:拒绝 live 模式、断言 transmitted()==0(既有代码,
  两层拒绝)。

## 4. 带宽 / 磁盘测算(出处:PLAN_DEPTH_EXPANSION §2–3,day-06 实测 L1 速率 ×
n=3 实测深度倍率括号 30–100×;字节 = 531 B/msg 实测)

| 规模 | 预期(30×) | 保守(100×) |
|---|---|---|
| 50 市场 | ~52 msg/s,**2.4 GB/天** | 峰 ~565 msg/s,**7.9 GB/天** |
| 200 市场 | ~176 msg/s,**8.1 GB/天** | 峰 ~1,584 msg/s,**27 GB/天** |

参照系:现有 firehose 28–32 GB/天(实测),EC2 盘余 91 GB(用 54%,
2026-07-11 ec2_health 读数),raw 保留 2 天(prune_raw,只删已封印日)。
判读:**Stage 1(50)无压力**;Stage 2(200)必须先用 probe 实测数字替换
30–100× 括号再算磁盘账——若保守值成真,先做 gzip-on-rotate(已在 BACKLOG)
或降 N。倍率括号只有 n=3(全是赛中 MLB,可能偏高),这正是先 probe 的理由。
S3 侧:raw 小时同步照旧,+2.4~8 GB/天 ≈ 累进 +$2~6/月/月,操作员月账可见。

## 5. seq 缺口检测(频道级,操作员点名项)

- **在线(已存在,零新代码)**:C++ 客户端 per-sid SidStream——seq 断档 ⇒
  整 sid 判失效 ⇒ 流内 `get_snapshot` 重锚;REST 快照永不回灌 WS 簿
  (kalshi_ws_protocol I1/I3/I4);WsRecorder 在 raw 里落
  gap/resync/loss/epoch_change 标记,ws_shadow metrics 报 resync_count。
- **离线(新增 tools/l2_gap_check.py)**:封印后按日核对——per-sid seq 连续性
  (orderbooks_full 已有 ws_sid/ws_seq 列,W5 落地)+ raw 标记统计 + 每市场
  快照重锚次数,写 `work/event_packs/l2_gaps_<D>.json` + quality_log 条目。
  缺口**计数并亮出**,绿灯不许撒谎(D2);研究侧按记录决定哪些窗口可用。

## 6. supervisor / 封印链集成点(为何不碰采集连续性)

1. supervisor 新增 **LAYER 1b(l2_shadow)**:可选层。启动条件 =
   l2_targets.csv 新鲜 + `work/live/l2_disable` 不存在(一键停用开关)。
   失败只退避重试 + 落盘告警,**永不阻塞 LAYER 1 firehose**;单实例锁独立。
2. **封印链零改动**:`seal_raw_files` 与 ingest 守护都按 `*.ndjson*` 全匹配
   (已核对源码),`l2_*` 文件自动进 check-caught-up / 导出 / 封印;
   `orderbooks_full` 表、ws_sid/ws_seq 列、zstd parquet 导出全部已存在。
   ⇒ L2 数据天生受与 firehose 同级的封印保护,不需要第二套账。
3. **capture_gaps 不动**(它只管 firehose 市场级静默);L2 质量由
   l2_gap_check 单独出记录,语义不同(定向订阅本来就该"只有被订市场有数据")。
4. **D4 纪律**:`l2_` 命名族与 ingest 侧测试同一变更落地;W03(hour-13 为何
   漏采发现)结论若涉及轮转命名,W06 实现必须先吸收。

## 7. 分阶段 rollout(每阶段 = 独立 W、独立审计后才部署、操作员逐阶段放行)

- **Stage 0 — probe(申请今天批准)**:`tools/depth_probe.py` **已在部署 tip**
  (registry 在册,操作员门控 `--operator-approved`,拒绝 live)。50 市场、
  900 秒、写 `work/probe/`(不进 raw、不进仓)。产出:按流动性档实测 msg/s 与
  bytes/市场,替换 30–100× 括号;顺带实测第二连接与 50 市场单订阅上限。
  预算 ≤0.3 GB 一次性、零 REST token。操作员照 PLAN_DEPTH_EXPANSION §5
  runbook 一条命令即可;probe 期间盯 freshness。
- **Stage 1 — N=50 跑 48h(probe 后的实现 W)**:LAYER 1b + l2_targets 选择器
  + l2_gap_check + 全套测试(D4/E1)。出口:连续 2 个含 L2 的日封印、gap 统计
  出报、firehose 零影响证据(capture_gaps 无异常 + 对照重启前后 freshness)。
- **Stage 2 — N≈200 稳态**:用实测重算磁盘/成本、定 gzip-on-rotate,
  操作员批准后扩容。

## 8. 红线自查

不碰采集连续性(独立进程/连接/日志族 + 可选层,firehose 路径零改动)·
不动 GUARDRAILS · 一场一个 W(本场只交规格,实现在批准后的新场)·
每阶段独立审计后才部署 · 只读永不下单(S1/S5;ws_shadow 双层拒绝 live)。

## 9. 请操作员裁决

1. [ ] **批准 Stage 0 probe**(推荐:15 分钟、零生产写入,给全案换上实测数字)
2. [ ] **批准三阶段框架**(每阶段仍逐个放行,本项只定路线不放行实现)
3. [ ] **四大类 = Tennis/MLB/Soccer/WNBA + 对照组构成**:认可 / 调整
