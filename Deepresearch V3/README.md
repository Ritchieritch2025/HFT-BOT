# Deepresearch V3 — 归档索引

deep03 全通道探索性研究（L1 / 成交 / L2 深度 / 市场图谱）的正式产出归档。

## 这是什么

- **性质**：探索层（OPEN_DISCOVERY）描述性研究，**不是可上实盘的策略**。
- **数据**：2026-07-10 至 07-17 共 8 个封存日、2,657 个精确 V3 对象、约 1.64 亿行。
- **运行**：D3-W2A-2026-07-19.12（runtime `5d5d278`），2026-07-22 10:25Z 完成，RUN_COMPLETE。
- **边界**：`candidate_or_profit_claim=false`、`EXPLORATORY_ONLY`——不含任何盈利/费后 PnL 声明；RFQ 为独立层待单独授权；L2 仅用质量干净的 07-12/15/17 三日。

## run_2026-07-22_D3-W2A/ 目录内容

| 文件 | 是什么 | 怎么看 |
|---|---|---|
| **REPORT/index.html** (35M) | 可视化报告，带图表 | 浏览器打开——最直观的入口 |
| **RESULTS.json** (4.3M) | 机器可读全量结果 | 四方法族 + L2 执行 + 市场图谱的完整数据 |
| RUN_COMPLETE.json | 完成收据 + 27 个产物哈希 | 证明这次运行的完整性 |
| FULLSCOPE_L2_REPORT_TABLES.json | L2 报告表 | 85.3万流动性事件、266万回补风险率、图谱、匹配对照 |
| FULLSCOPE_MARKET_GRAPH_RESULT.json | 市场关系图谱 | 44.9万"市场×日"节点 |
| DATA_QUALITY_RECEIPT.json | 数据质量收据 | 哪些日期干净/隔离及原因 |
| EXCLUSION_WATERFALL.json | 剔除瀑布 | 每一行数据的去留账本 |
| ESTIMABILITY_PREFLIGHT.json | 可估性预检 | 哪些方法能算/不能算 |
| L2_INDEPENDENT_AUDIT_REPORT.md / _RECEIPT.json | L2 独立审计 | 证明 L2 算法算得对 |
| REPRODUCTION_RECEIPT.json | 复算收据 | 结果可复现的凭证 |

## 关键发现（探索层，未验证）

- **B01 逆向选择曲面**：✅ 完成 · **B02 单边生存**：✅ · **B04 活动节奏**：✅ · **B03 跨市场**：数据不足，NOT_ESTIMABLE
- **L2 流动性显微镜**：85.3 万条撤退/耗尽/回补事件 + 266 万条回补风险率（干净 3 日）
- **行数守恒**：164,059,713 期望 = 164,059,713 实际，一字不差
- 最值得深挖：**266 万条回补风险率** = deep02 头号候选（88-93¢ 拉锯双边报价）点名要的"深度蒸发信号"

## 下一步（不能跳）

这是勘探地图，不是策略。判断"能不能赚"要走三闸漏斗（TRAIN→VALIDATION→CONFIRMATION），硬门是 ≥20 个独立封存日——现有 8 个，约月底到位。届时候选拿**没见过的新数据**重考，幸存者才配进影子/微实盘。
