# Visualize-Everything 裁决(执行副本,生产/管道分支)

正本:主仓(plan-sports-market-dynamics-v2 分支)
`docs/PLAN_SPORTS_TRADING_DECISIONS.md` **D-3** 条目(索引:
`docs/research_notes/DOCTRINE_LEDGER.md` DOC-1)。本副本供管道侧报告
生成器遵循;两处若有出入以 D-3 正本为准。

## 操作员常设裁决原文(VERBATIM,2026-07-12)

> Operator standing ruling (doctrine ledger, upgrade to Visualize-Everything): NO scalar statistic may be reported alone. Every reported statistic ships with its full distribution plot (histogram or ECDF) marked with p50/p99/max and n. Inherently-scalar quantities (shares, counts) instead show their distribution across the natural unit (per-hour/per-event) or a bootstrap CI plot. Bimodality, long tails, and discontinuities MUST be named and explained in the caption. Definition block (plain meaning + formula + provenance + code location) and tier banner on every chart. Applies to ALL segments, ALL reports, starting with the 24h observation window and the RFQ 48h report.

## 管道侧待改造(BACKLOG B7)

1. `tools/research/rfq_flow_report.py`(48h 报告):现有 index.html 图表
   补齐——每统计量配直方图/ECDF(标 p50/p99/max/n)、占比类改按小时分布、
   图注点名双峰/长尾/断点、每图定义块 + 层级横幅。
2. L2 Stage-1 24h 观察窗报告(尚未开工):从第一版起按本裁决设计。
