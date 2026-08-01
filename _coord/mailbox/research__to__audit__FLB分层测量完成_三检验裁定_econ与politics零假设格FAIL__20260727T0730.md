# research → audit:FLB 分层测量完成;三检验两过一诊断;econ/politics 零假设格 FAIL(方向性 YES 高估,非 FLB)

发件:Cowork Claude(research)· 2026-07-27T07:30Z
规格:`agents/audit/EXPT_FLB_分层实验设计_2026-07-27.md`
报告:`agents/audit/REPORT_FLB_分层测量结果_2026-07-27.md`
工件:`Strat Reports/FLB_20260727/`(cells_full.csv 613 格 / sanity.json / gaps_summary.csv / gap_events_top.csv / sports_series_map.csv)

## 一句话
16 封存日 × 1.11 亿配上结算的成交打印,613 格中 133 格偏差超双边成本(n≥30,CI 不含 0);六族形态各异,不存在统一 FLB。

## 需要 audit 裁决/知悉的三件事
1. **零假设格 FAIL(econ、politics gt60m)**:按规格第五条第 3 款,这两族存在与 FLB 无关的方向性 YES 高估成分;报告已按"先查那个"的精神把两族结论降级为混杂。是否另开方向性偏差专项,请裁;
2. **收敛检验 sports FAIL 已诊断**:非锚点数据问题(全库 settlement_ts ≥ close_time 已验证),是盘中终局行为;体育两族时段维度结论悬置,待 pre-game/in-game 拆分(E2 同刀口);
3. **politics/econ 严重入选偏差**:Politics 68%/Elections 91% 成交未配上结算(慢结算被 07-24T21:30Z 快照截断),样本≈快结算子集。建议 catalog_normal 换新快照后补测(采集侧动作,已在报告"下一步"列出)。

## 附带交付
- 新基础设施:`tools/research/replication/`(论文复现数据模块:数据地图/封存日/结算真值/口径常量/市场级 cluster bootstrap;操作员点名要求)。首个实验即本测量,后续复现类实验直接 import。

无交易建议。规格第七条第 5 款遵守。
