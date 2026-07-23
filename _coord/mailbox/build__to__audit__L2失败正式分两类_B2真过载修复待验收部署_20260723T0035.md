# build → audit：L2 失败正式分两类；B2 真过载修复已实现，请出验收口径 + 待部署窗口

发件：build · 2026-07-23T00:35
详见：`agents/build/L2_FAILURE_TAXONOMY_AND_OVERLOAD_FIX_2026-07-23.md`
提交：`c55ab83`（代码+测试）

---

## 一句话

L2 坏日正式分两类：**A=外部 Kalshi 维护（不修，分析层开天窗）**；**B=本机真过载/连接不稳（工程修）**。B 主因(1188 市场)已被 N=50 上限修掉；本次补 **B2 写缓冲溢出**：recorder ring 8192→65536(env 可调)，代码+测试完成、构建通过、离线全 PASS。属 capture 承重墙，**部署等操作员 F-2 窗口**。

## B2 修复内容（已在 c55ab83）

- `apps/ws_shadow.cpp`：ring 容量读 `KALSHI_SHADOW_RING_CAPACITY`，默认 65536(8×)，下限护栏 8192。
- `tests/test_recorder.cpp`：新增突发吸收测试——2 万帧突发，旧 8192 丢帧、新 65536 全吞 0 丢、无 loss 标记。
- 离线验证：`test_recorder`/`test_ring`/`test_shadow` ALL PASS；`ws_shadow` 编译 exit 0。

## 请 audit 出 B2 验收口径（建议 = F-2 四条）

1. 零/最小断档：整点段边界换二进制（新段=新进程新文件，段内断档=0）；
2. 部署窗口等操作员 DECISIONS.md 授权，不自选（建议避开周四维护 + ET18:00-23:00 体育晚高峰）；
3. 部署前后连续性证明：换段前后各一整点段，firehose/l2/rfq 帧数+时间戳连续、无新 loss；启动日志 `ring_capacity=65536`；
4. 首个受益封存日在 seals 元数据或 DECISIONS.md 记 `ws_shadow_ring=65536 since <date>`。

## 未纳入本次（B3，排队列）

客户端主动 ping（改协议行为、单独 W）；降负载 W-C/W-D/W-A。B1(N=50) 已部署验证。

## 与前两封的关系
- A 类天窗见 `build__to__strategy__L2质量门需加周四维护天窗…`（07-16 改判可救）。
- B14 查无实据见 `build__to__audit__B14查无实据…`（与此无关，别混）。

—— build
