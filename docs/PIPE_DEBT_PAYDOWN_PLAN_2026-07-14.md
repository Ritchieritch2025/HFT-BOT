# PIPE — 卡顿债还清计划 (2026-07-14, execution-ready)

目标:清掉数据管道的结构性卡顿,让数据面**稳且快**,从而安全开启 auto-research。
背景与债务台账见 `PIPE_INCIDENT_AND_PLAN_2026-07-14.md`。执行纪律:**一个 W 一个
fresh session,改完独立审计,退出仪式**(项目标准)。按杠杆排序,W-A 优先。

## 恶性循环(为什么 W-A 三合一)
```
封印链抢不过 staging 写锁(B11) → 封印慢 → 修剪滞后(B16) → staging 膨胀
  → 启动重建 O(全表 L1) 变慢/OOM(B15) → 入库追不平 → 封印更慢 ↺
```
2026-07-14 的马拉松就是这个循环转了一整天。B11/B15/B16 互相喂,必须一个协调 W 一起修。

## W-A(最高杠杆):打破封印↔staging 循环 [B11+B15+B16]
> **状态 2026-07-14 23:0xZ:BUILT + 测试全绿,分支 `w-a-seal-staging-loop`
> @ 0bfc347(已推 ec2 裸仓库)。未部署 —— 等 07-14 封印正常落地 + 独立
> 审计后再切生产树 + 重启 supervisor(重启步骤见 SESSION_LOG 当日条目)。
> 实现要点:B11 = tools/ingest_guard.sh(进程表为真、pidfile 只是提示;
> verified-stop 杀全部含孤儿、start 拒绝 pause/收养孤儿/spawn 后复查),
> B15 = _rebuild_state 只扫 max(ts)-24h 窗口(心跳可达市场全保留,等价性
> 有测试),B16 = export_day --prune-sealed(封印当轮清 staging,扫全部
> 已封旧日自愈积压),契约测试 tests/test_ingest_guard.py 4 项 +
> test_pipeline_contract 新增 3 项。
文件:`tools/pipeline_supervisor.sh` (run_seal_chain / stop_ingest)、`tools/ingest.py`
(_rebuild_state)、`tools/export_day.py` (prune)。
- **B11**:封印链在**整段 export+seal** 期间硬保持 ingest 停止——own-pause token +
  验证进程真的停了(verified stop,照 `recover_rfq_seal` clauses 16-18 的做法),
  绝不让 ingest 在导出中途抢回写锁。加 contract test:导出全程 ingest 不复活。
- **B15**:`_rebuild_state` 改**增量/有界**——不再对整个 orderbooks_l1 做窗口查询。
  方案(择一,倾向 a):(a) 只从**最近一天**的 L1 分区重建 last-state-per-market
  (状态只需最新值,历史无关);(b) 持久化一张小的 `l1_state` 表、增量更新,启动直接
  读。验收:正常 staging 上启动重建 < 1 分钟。回归夹具:重建结果与全表版一致。
- **B16**:每次封印后**当轮就把已封日从 staging 修剪掉**(今天 07-12 封了却赖在库里
  没修剪 → 库胀)。验收:sealed 日在封印后一个周期内从 staging 消失。
- **W-A 验收**:一整天封印全程 ingest 不丢锁;重启重建 < 1 分钟;封印后即修剪。**这一个
  W 根除今天这种马拉松。**

## W-B:修采集端坏时钟 [B14 + B17] (C++ ws_shadow)
文件:`apps/ws_shadow.cpp`(L2 帧的 recv 时钟戳)。
- 定位 ws_shadow 给 L2 `orderbook_delta` 帧盖 `recv_mono_ns` 的地方,修那个垃圾巨值
  (~2e19..2e24,疑似未初始化/溢出/错字段)。入库侧守卫保留做纵深防御。
- 顺带查 **B17**:未来日期 ts(落在下一 UTC 日、但在合理性窗口内)——很可能同一采集
  时钟家族;先从 raw 扫一条样本钉死是 exchange ts_ms 还是 recv 时钟坏的。
- 验收:新 L2 采集里 recv_mono_ns 全部合理;入库 `bad_value` 保持 0;无未来日期行。

## W-C:入库吞吐/并行 [B9]
文件:`tools/ingest.py`。
- 现状:单进程 ~1.5 核、吞吐仅比采集快 ~1.2×,余量薄。
- 方案:按文件族并行入库(firehose/l2/rfq 各 worker),或多线程解析;RFQ 快通道已在。
- 验收:入库吞吐 > 采集 1.5×,用满多核;积压不再累积。

## W-D:采集-导出解耦(硬伤)
文件:`tools/pipeline_supervisor.sh`(Layer-3 导出跑在 Layer-1 采集循环里)。
- 把导出/封印移出采集循环(后台 worker / 独立进程,尊重 DuckDB 单写者),或至少在导出
  前先 respawn ws_shadow。
- 验收:导出期间采集零间隙(现状 02:00Z 二次导出停采集 ~8 分钟)。

## auto-research 前置(W-A 完成后)
数据面稳(W-A)**+** 二选一:
- ① **B5 研究内存护栏**:research 链外面套 cgroup/systemd MemoryMax + DuckDB
  memory_limit,最坏只杀研究、不波及采集/入库 → 然后重开箱上 `AUTO_RESEARCH=1`;
- ② 走 **Mac 侧 W05 研究桥**(箱上研究保持熔断 HOTFIX-02)。
**W-A 修完 + B5 护栏 = 可安全开 auto-research。**

## 收尾杂项(随手折叠)
- B17(07-15 未来日期来源,并入 W-B)· B8(researchReader 密钥轮换)·
  07-09 补封或放弃(操作员裁决)· 12GB staging 文件 VACUUM 缩盘(可选)。

## 建议执行顺序
**W-A → B5 护栏 →(此时可开 auto-research)→ W-B → W-C → W-D。**
W-A 是解锁一切的钥匙;B5 护栏是 auto-research 的闸;W-B/C/D 是把管道从"能跑"提到
"跑得稳且快"。速战速决就按这个序、一个 fresh session 一个 W。
