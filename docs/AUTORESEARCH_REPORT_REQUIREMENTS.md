# AUTORESEARCH 审阅页需求(操作员口述,2026-07-15,E2 落盘)

操作员要求:研究结果汇总到**一个单独网页**,操作员**逐一过目**每个结论。
这是对 SPORTS-AUTORESEARCH-01 交付形态的细化,不改已钉定的 V2 图纸
(sha256 c4c31674…6342c6);发布任务时作为报告规范附加项一并生效。

## 硬要求

1. **单页自包含**:一个 `REPORT/index.html`,内联 CSS/JS、图表以 data-URI
   或内联 SVG 嵌入,零外部依赖 —— 不开服务器、断网也能看(和图纸
   REPORT/index.html 一致,升级为唯一审阅入口)。W09 产出后同步到 Mac /
   发成 Artifact 供操作员在 claude.ai 审阅。
2. **逐一过目结构**:顶部假设索引(全部 tested / rejected / COLLECT_MORE /
   DATA_STARVED / PROMOTION_READY,按状态可筛),每条下钻到独立档案:
   假设陈述 → 数据覆盖 → 特征 → 统计检验(含多重校正、bootstrap CI、
   负控制)→ 裁决 → 为何 edge 可能持续 → 反证/被否理由。**被否与负结果
   与赢家同等展示**(反"只数赢的"自欺,图纸 §16 精神)。
3. **每个数字带出处**:release_id + exact VersionId + 查询 + 行数,可复现。
4. **层级横幅强制**(操作员 SUPERSEDE 纪律):每个产物顶部挂
   `SEALED_PENDING_QUALITY_ASSESSMENT` + `EXPLORATORY_ONLY` 横幅;
   禁 freeze / verdict / promotion / live-candidate 任何字样冒充确认级。
5. **四标签不混**(图纸 §4):证据层级 / 时钟纪律 / 实验切分 / 结论状态
   四套标签在页面上分列,永不互相冒充。
6. **绿色不撒谎**:排除瀑布(inclusion→exclusion 每步计数与理由)、
   数据覆盖立方体、缺口/gap 状态都上页面;没有"一片绿"掩盖降级数据。

## 落地时点

报告生成阶段(autoresearch 跑完)由 W09 产出 `REPORT/index.html`;
审阅页可用 Artifact 机制发到 claude.ai 供操作员逐条批注。**现在不建**
(无数据);此文件是发布时的报告规范锚点。相关:
`W05_PUBLICATION_STATUS_2026-07-14.md`(层级纪律)、
`SPORTS_AUTORESEARCH_01_MISSION_TEXT_V2_2026-07-15.md`(§2 报告工件清单)。
