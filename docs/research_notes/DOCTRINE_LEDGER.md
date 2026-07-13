# DOCTRINE_LEDGER — 报告与研究方法论裁决索引

方法论类操作员裁决的**索引**(非正本)。按"单一台账、先查重再编号"房规:
凡已在 `docs/PLAN_SPORTS_TRADING_DECISIONS.md` 落档的裁决,此处只留指针,
原文以 DECISIONS 为唯一权威家。

---

## DOC-1 → D-3 · 2026-07-12 — Visualize-Everything(全报告证据规则)

**正本:`docs/PLAN_SPORTS_TRADING_DECISIONS.md` D-3 条目(操作员原文 +
归档注记)。** 一句话:禁止裸报单个统计量——每个统计量配完整分布图
(histogram/ECDF,标 p50/p99/max/n);天生单值的量按自然单位展开或给
bootstrap CI;双峰/长尾/断点必须图注点名(未观察到也须写明);每图带
定义块(白话+公式+出处+代码位置)与 tier banner。首批适用:24h 观察窗
报告 + RFQ 48h 报告。不达标产物不得标 COMPLETE。

执行副本(管道分支,供 rfq_flow_report / L2 观察报告生成器遵循):
recovery 分支 `docs/DOCTRINE_VISUALIZE_EVERYTHING.md`;工程改造项 =
recovery BACKLOG B7。

## DOC-2 · 2026-07-13 — 凭证零回显硬规则(操作员裁决,原文)

> HARD RULE going forward: never print/echo/cat/awk any credential value — source-and-use only, connectivity checks report pass/fail only.

归档注记:背景 = W05 Step-0 期间编排会话修复 env 文件时经 awk 将
researchReader 双值回显进会话转录(S4 违规,已如实上报);操作员裁决
接受该只读研究前缀密钥的残余暴露风险、暂不轮换,并立此硬规则。
轮换已排入下次控制台会话(recovery BACKLOG B8,非阻塞)。
